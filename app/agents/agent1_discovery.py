"""Agente 1 — percorre o banco legado e produz o DiscoveryReport.

A parte determinística (listar tabelas, rodar profile_column) é código
comum. O LLM entra para: nomear o que a tabela/coluna provavelmente
representa, cruzar padrões de valor com o nome da coluna, e redigir os
"risks" em linguagem legível para quem vai revisar depois.

Ordem das operações, que é o que garante a LGPD:

1. **perfilar tudo primeiro** — só leitura local, nenhum LLM envolvido;
2. **decidir se há PII** pelos nomes e pela forma dos valores;
3. **aplicar a política de backend** — havendo PII, só endpoint local;
4. **mascarar** `top_values` e enviar o *padrão*, nunca o valor.

O passo 3 precisa vir depois do 1 porque é o perfilamento que revela a PII;
e antes do 4/LLM porque a primeira chamada é o ponto sem volta.
"""
from __future__ import annotations

from sqlalchemy.engine import Engine

from app.db import introspection as tools
from app.llm.client import LLMClient
from app.privacy.masking import pii_signal
from app.privacy.policy import enforce_pii_backend_policy
from app.privacy.report import mask_stats, pii_columns_from_stats, sanitize_profile
from app.schemas.profiling import DiscoveryReport, TableProfile

SYSTEM_PROMPT = """\
Você é um engenheiro de dados especialista em bancos legados de sistemas
de gestão pública brasileira.

Você recebe, para UMA tabela: suas colunas, tipos, e estatísticas brutas
de perfilamento (nulos, cardinalidade, valores mais frequentes).

Os valores de amostra chegam MASCARADOS de propósito: `#` representa um
dígito, `A` uma letra, e padrões como `###.###.###-##` ou `DD/MM/AAAA` são a
FORMA do valor, não o valor. Use a forma para inferir o tipo e nunca tente
reconstruir o dado original. `pii_name_hint`/`pii_value_hint` indicam que o
nome ou a forma já sugerem dado pessoal.

Sua tarefa: para cada coluna, inferir o que ela provavelmente representa,
sinalizar problemas de qualidade/formato, e marcar pii=true se houver
chance razoável de ser dado pessoal (CPF, nome, endereço, telefone,
e-mail, data de nascimento, matrícula).

Nunca conclua o significado de uma coluna só pelo nome — baseie-se nos
padrões de amostra fornecidos. Em caso de dúvida real, descreva a dúvida
em vez de assumir uma resposta.

Liste em "risks" qualquer achado que precise de validação humana antes
da migração (ex.: relacionamento implícito não declarado, campo
reaproveitado para mais de um propósito, taxa alta de inconsistência).
"""


def _profile_column(engine: Engine, table: str, column: str) -> dict:
    """Perfil + as duas pistas de PII, calculadas em cima do valor real."""
    stats = tools.profile_column(engine, table, column)
    stats["pii_name_hint"] = tools.detect_pii_by_name(column)
    stats["pii_value_hint"] = pii_signal(stats.get("top_values"), column)
    return stats


class DiscoveryAgent:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def run(self, engine: Engine, dialect: str, tables: list[str] | None = None) -> DiscoveryReport:
        table_names = tables or tools.list_tables(engine)

        # 1. Perfilamento inteiro, local, sem LLM.
        raw_stats: dict[str, list[dict]] = {}
        for table in table_names:
            raw_stats[table] = [
                _profile_column(engine, table, col["name"])
                for col in tools.get_schema(engine, table)
            ]

        # 2/3. A PII é conhecida antes da primeira chamada; a política decide
        # se o backend configurado pode continuar.
        pii_columns = pii_columns_from_stats(raw_stats)
        enforce_pii_backend_policy(bool(pii_columns))

        # 4. Só o padrão entra no prompt.
        profiles: list[TableProfile] = []
        for table, stats in raw_stats.items():
            safe_stats = [mask_stats(item) for item in stats]
            row_count = safe_stats[0]["row_count"] if safe_stats else 0

            user_prompt = (
                f"Tabela: {table}\n"
                f"Colunas e estatísticas brutas (JSON): {safe_stats}\n\n"
                "Produza o TableProfile completo para esta tabela."
            )

            profile = self.llm.complete_structured(
                SYSTEM_PROMPT, user_prompt, TableProfile
            )
            # Garantias determinísticas por cima da resposta do modelo
            profile.table = table
            profile.row_count = row_count
            # Defesa em profundidade: o artefato gravado também não carrega
            # valor real, mesmo que o modelo tenha repetido algo.
            profiles.append(sanitize_profile(profile))

        return DiscoveryReport(
            source_dialect=dialect,
            tables=profiles,
            pii_columns=pii_columns,
        )
