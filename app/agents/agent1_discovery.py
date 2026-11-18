"""Agente 1 — percorre o banco legado e produz o DiscoveryReport.

A parte determinística (listar tabelas, rodar profile_column) é código
comum. O LLM entra para: nomear o que a tabela/coluna provavelmente
representa, cruzar padrões de valor com o nome da coluna, e redigir os
"risks" em linguagem legível para quem vai revisar depois.
"""
from __future__ import annotations

from sqlalchemy.engine import Engine

from app.db import introspection as tools
from app.llm.client import LLMClient
from app.schemas.profiling import DiscoveryReport, TableProfile

SYSTEM_PROMPT = """\
Você é um engenheiro de dados especialista em bancos legados de sistemas
de gestão pública brasileira.

Você recebe, para UMA tabela: suas colunas, tipos, e estatísticas brutas
de perfilamento (nulos, cardinalidade, valores mais frequentes).

Sua tarefa: para cada coluna, inferir o que ela provavelmente representa,
sinalizar problemas de qualidade/formato, e marcar pii=true se houver
chance razoável de ser dado pessoal (CPF, nome, endereço, telefone,
e-mail, data de nascimento, matrícula).

Nunca conclua o significado de uma coluna só pelo nome — baseie-se nos
valores de amostra fornecidos. Em caso de dúvida real, descreva a dúvida
em vez de assumir uma resposta.

Liste em "risks" qualquer achado que precise de validação humana antes
da migração (ex.: relacionamento implícito não declarado, campo
reaproveitado para mais de um propósito, taxa alta de inconsistência).
"""


class DiscoveryAgent:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def run(self, engine: Engine, dialect: str, tables: list[str] | None = None) -> DiscoveryReport:
        table_names = tables or tools.list_tables(engine)
        profiles: list[TableProfile] = []

        for table in table_names:
            schema = tools.get_schema(engine, table)
            raw_stats = []
            for col in schema:
                stats = tools.profile_column(engine, table, col["name"])
                stats["pii_name_hint"] = tools.detect_pii_by_name(col["name"])
                raw_stats.append(stats)

            row_count = raw_stats[0]["row_count"] if raw_stats else 0

            user_prompt = (
                f"Tabela: {table}\n"
                f"Colunas e estatísticas brutas (JSON): {raw_stats}\n\n"
                "Produza o TableProfile completo para esta tabela."
            )

            profile = self.llm.complete_structured(
                SYSTEM_PROMPT, user_prompt, TableProfile
            )
            # Garantias determinísticas por cima da resposta do modelo
            profile.table = table
            profile.row_count = row_count
            profiles.append(profile)

        return DiscoveryReport(source_dialect=dialect, tables=profiles)
