"""Aplica a máscara aos artefatos do discovery antes de qualquer LLM.

Duas fronteiras, as duas necessárias:

1. **antes de enviar** — `mask_stats()` substitui `top_values` reais pelo
   padrão, e é o que entra no prompt do Agente 1;
2. **antes de gravar/reutilizar** — `sanitize_profile()` e
   `sanitize_discovery()` garantem que `discovery_report.json` (e qualquer
   relatório antigo relido no `map`) também não carregue valor real. Assim o
   Agente 2, que lê o relatório do disco, herda a mesma garantia.

A detecção é deliberadamente conservadora: nome de coluna que sugere PII ou
valor com forma de documento/e-mail já basta (data sozinha é dado de negócio;
`DT_NASC` é pega pelo nome). Falso positivo custa uma máscara a mais; falso
negativo custa dado pessoal na rede.
"""
from __future__ import annotations

from app.privacy.masking import (
    is_pii_name,
    mask_free_text,
    mask_value,
    mask_values,
    pii_signal,
)
from app.schemas.profiling import ColumnProfile, DiscoveryReport, TableProfile

# Quantos valores são inspecionados para decidir se a coluna tem PII. O
# perfilamento devolve no máximo 10; o limite protege contra entrada manual.
_SIGNAL_SAMPLE = 10


def stats_have_pii(stats: dict) -> bool:
    """Indício de PII nas estatísticas brutas de uma coluna."""
    if stats.get("pii_name_hint") or stats.get("pii_value_hint"):
        return True
    return pii_signal(stats.get("top_values") or [], str(stats.get("column") or ""))


def pii_columns_from_stats(raw_stats: dict[str, list[dict]]) -> list[str]:
    """`["CAD_PESSOA.CPF", ...]` — o que vai para a auditoria."""
    found: list[str] = []
    for table, columns in raw_stats.items():
        for stats in columns:
            if stats_have_pii(stats):
                found.append(f"{table}.{stats.get('column', '?')}")
    return sorted(found)


def mask_stats(stats: dict) -> dict:
    """Cópia das estatísticas com `top_values` trocados por padrões.

    Não mexe no original: o valor real fica só na memória do processo e
    desaparece depois do perfilamento.
    """
    masked = dict(stats)
    masked["top_values"] = mask_values(
        [str(value) for value in (stats.get("top_values") or [])],
        str(stats.get("column") or ""),
    )
    return masked


def _masked_text(text: str, pairs: list[tuple[str, str]]) -> str:
    """Substitui valores conhecidos no texto e depois varre PII por forma.

    Ordem importa: primeiro os valores exatos que o relatório carrega (um
    nome que o LLM citou numa nota), do mais longo para o mais curto; depois
    a varredura genérica de documento/e-mail/telefone/data.
    """
    for value, pattern in pairs:
        text = text.replace(value, pattern)
    return mask_free_text(text)


def _table_pairs(profile: TableProfile) -> list[tuple[str, str]]:
    """Pares `valor real -> padrão` das colunas de uma tabela.

    Usado para limpar notas e riscos: um relatório antigo pode ter citado o
    valor de uma coluna em texto livre. Valores que já são padrão produzem
    `pattern == value` e não entram.
    """
    pairs: set[tuple[str, str]] = set()
    for column in profile.columns:
        for raw in column.top_values:
            value = str(raw).strip()
            if not value:
                continue
            if not (is_pii_name(column.name) or pii_signal([value], column.name)):
                continue
            pattern = mask_value(value, column.name)
            if pattern and pattern != value:
                pairs.add((value, pattern))
    return sorted(pairs, key=lambda item: len(item[0]), reverse=True)


def sanitize_profile(profile: TableProfile) -> TableProfile:
    """Mascara `top_values`, notas e riscos de um perfil já validado.

    Roda depois do LLM: mesmo que o modelo repita um valor (não deveria,
    porque só viu padrões), o artefato gravado não o carrega.
    """
    pairs = _table_pairs(profile)
    for column in profile.columns:
        column.top_values = mask_values(column.top_values, column.name)
        column.notes = _masked_text(column.notes, pairs)
    profile.risks = [_masked_text(risk, pairs) for risk in profile.risks]
    return profile


def sanitize_discovery(report: DiscoveryReport) -> DiscoveryReport:
    """Devolve uma cópia integralmente mascarada do relatório de discovery.

    Chamado no `map` antes de montar o prompt: cobre tanto o relatório recém
    gerado quanto um `discovery_report.json` antigo, anterior à Fase 4.
    """
    clean = report.model_copy(deep=True)
    for table in clean.tables:
        pairs = _table_pairs(table)
        for column in table.columns:
            column.top_values = mask_values(column.top_values, column.name)
            column.notes = _masked_text(column.notes, pairs)
        table.risks = [_masked_text(risk, pairs) for risk in table.risks]
    return clean


def _column_has_pii(column: ColumnProfile) -> bool:
    if column.pii or is_pii_name(column.name):
        return True
    return pii_signal(column.top_values[:_SIGNAL_SAMPLE], column.name)


def discovery_pii_columns(report: DiscoveryReport | None) -> list[str]:
    """Colunas com PII declarada pelo Agente 1 ou inferida pela forma/valor.

    Une o que o perfilamento viu (campo `pii_columns`, gravado antes de
    mascarar) com o que o modelo marcou (`pii=true`) e com a forma dos
    valores que ainda estiverem no relatório. A união importa para relatórios
    antigos e para o caso em que o LLM não marcou uma coluna que a heurística
    pegou.
    """
    if report is None:
        return []
    found = set(report.pii_columns)
    found.update(
        f"{table.table}.{column.name}"
        for table in report.tables
        for column in table.columns
        if _column_has_pii(column)
    )
    return sorted(found)
