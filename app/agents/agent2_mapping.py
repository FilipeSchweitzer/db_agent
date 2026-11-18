"""Agente 2 — propõe o mapeamento origem -> destino.

Produz um MappingDocument completo, mas NUNCA o considere pronto para uso: o
checkpoint humano (app/review/export.py + aprovação manual) é obrigatório
antes de qualquer entrada seguir para o Agente 3.

O mapeamento é feito **uma tabela de destino por chamada**. Duas razões:

1. tamanho — um schema de destino real não cabe numa resposta só, e o JSON
   truncado era o principal motivo de falha da fase;
2. qualidade — o prompt de cada chamada fala de uma tabela e do índice
   compacto de colunas da origem, em vez de despejar o relatório inteiro de
   discovery (que cresce com o número de tabelas do legado).
"""
from __future__ import annotations

import uuid
from collections.abc import Callable

from app.llm.client import LLMClient
from app.schemas.mapping import MappingDocument, MappingEntry, TableMapping
from app.schemas.profiling import DiscoveryReport
from app.schemas.target_schema import TargetSchema, TargetTable
from app.transforms.registry import available_transform_names

# Quantos caracteres de observação por coluna entram no índice de origem.
_NOTE_LIMIT = 90
# Quantos riscos por tabela entram no índice de origem.
_RISK_LIMIT = 3

SYSTEM_PROMPT = """\
Você mapeia campos de um banco legado para o schema de destino de um
sistema de gestão municipal.

Você recebe UMA tabela de destino por vez, o índice de colunas disponíveis no
banco legado e, se necessário, uma transformação — escolhida OBRIGATORIAMENTE
entre os tipos de transformação disponíveis listados abaixo. Nunca invente um
tipo de transformação novo.

Transformações disponíveis: {transform_names}

Regras:
- Devolva uma entrada para cada campo de destino da tabela recebida.
- Campos marcados pii=true no schema de destino SEMPRE recebem
  requires_human_review=true, mesmo com confiança alta.
- Se não encontrar correspondência razoável, não force uma: inclua o campo em
  unmapped_target_fields (formato `tabela.campo`) e não crie entrada para ele.
- confidence="low" ou "medium" sempre vem com review_reason preenchido.
- Use somente colunas listadas no índice de origem; não invente nomes.
"""

ProgressCallback = Callable[[int, int, str], None]


class MappingAgent:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def run(
        self,
        project: str,
        discovery: DiscoveryReport,
        target_schema: TargetSchema,
        on_table: ProgressCallback | None = None,
    ) -> MappingDocument:
        system_prompt = SYSTEM_PROMPT.format(
            transform_names=", ".join(available_transform_names())
        )
        source_index = build_source_index(discovery)

        total = len(target_schema.tables)
        entries: list[MappingEntry] = []
        unmapped: list[str] = []
        warnings: list[str] = []

        for position, (table, spec) in enumerate(target_schema.tables.items(), start=1):
            if on_table is not None:
                on_table(position, total, table)

            partial = self.llm.complete_structured(
                system_prompt,
                _user_prompt(table, spec, source_index),
                TableMapping,
            )
            table_entries, table_warnings = _normalize(table, spec, partial)
            entries.extend(table_entries)
            unmapped.extend(partial.unmapped_target_fields)
            warnings.extend(table_warnings)

        entries.sort(key=_schema_order(target_schema))
        missing = _missing_target_fields(target_schema, entries, unmapped)
        if missing:
            warnings.append(
                f"{len(missing)} campo(s) de destino ficaram sem entrada: " + ", ".join(missing)
            )

        return MappingDocument(
            mapping_id=str(uuid.uuid4()),
            project=project,
            entries=entries,
            unmapped_target_fields=sorted(set(unmapped) | set(missing)),
            warnings=warnings,
        )


def _user_prompt(table: str, spec: TargetTable, source_index: str) -> str:
    return (
        f"Tabela de destino: {table}\n\n"
        f"Campos de destino (JSON) — mapeie TODOS: {spec.model_dump_json()}\n\n"
        f"{source_index}\n\n"
        f"Produza os mapeamentos dos campos de '{table}'."
    )


def build_source_index(discovery: DiscoveryReport) -> str:
    """Índice compacto da origem: uma linha por coluna, em vez do JSON inteiro.

    O relatório completo de discovery repete, para cada coluna, contagem de
    nulos, cardinalidade e top-10 valores. Isso é útil para o Agente 1 decidir
    o que a coluna é, e ruído para o Agente 2 escolher correspondência — que
    precisa é saber que a coluna existe e o que ela provavelmente significa.
    """
    lines = [
        "Colunas disponíveis no banco legado"
        " (tabela.coluna [tipo inferido] PII? — observação):"
    ]
    for table in discovery.tables:
        for column in table.columns:
            note = " ".join((column.notes or "").split())
            if len(note) > _NOTE_LIMIT:
                note = note[: _NOTE_LIMIT - 1] + "…"
            flags = " PII" if column.pii else ""
            suffix = f" — {note}" if note else ""
            lines.append(f"- {table.table}.{column.name} [{column.inferred_type}]{flags}{suffix}")
        for risk in table.risks[:_RISK_LIMIT]:
            lines.append(f"- (risco em {table.table}) {risk}")

    if len(lines) == 1:
        lines.append("- (nenhuma coluna foi perfilada)")
    return "\n".join(lines)


def _normalize(
    table: str,
    spec: TargetTable,
    partial: TableMapping,
) -> tuple[list[MappingEntry], list[str]]:
    """Aplica as garantias determinísticas por cima da resposta do modelo."""
    warnings: list[str] = []
    accepted: list[MappingEntry] = []
    seen: set[str] = set()

    for entry in partial.entries:
        if entry.target_field not in spec.fields:
            warnings.append(
                f"{table}: o modelo mapeou '{entry.target_field}', que não existe no "
                "schema de destino — ignorado."
            )
            continue
        if entry.target_field in seen:
            warnings.append(f"{table}.{entry.target_field}: entrada duplicada ignorada.")
            continue

        seen.add(entry.target_field)
        # A tabela vem do schema, não do modelo: evita entrada no lugar errado.
        entry.target_table = table
        accepted.append(entry)

    return accepted, warnings


def _schema_order(target_schema: TargetSchema) -> Callable[[MappingEntry], tuple[int, int]]:
    """Ordena as entradas na ordem do schema de destino (diff estável no xlsx)."""
    table_position = {name: index for index, name in enumerate(target_schema.tables)}
    field_position = {
        (name, field): index
        for name, table in target_schema.tables.items()
        for index, field in enumerate(table.fields)
    }

    def key(entry: MappingEntry) -> tuple[int, int]:
        return (
            table_position.get(entry.target_table, len(table_position)),
            field_position.get((entry.target_table, entry.target_field), 10_000),
        )

    return key


def _missing_target_fields(
    target_schema: TargetSchema,
    entries: list[MappingEntry],
    unmapped: list[str],
) -> list[str]:
    """Campos do destino que o modelo simplesmente esqueceu de citar."""
    covered = {(entry.target_table, entry.target_field) for entry in entries}
    declared = set(unmapped)
    missing: list[str] = []
    for table, spec in target_schema.tables.items():
        for field in spec.fields:
            qualified = f"{table}.{field}"
            if (table, field) in covered or qualified in declared or field in declared:
                continue
            missing.append(qualified)
    return missing
