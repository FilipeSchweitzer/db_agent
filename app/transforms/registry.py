"""Lista fechada de transformações conhecidas.

O Agente 2 só pode escolher transformações daqui — nunca gera lógica livre.
Se nenhuma entrada servir, o campo é marcado requires_human_review=True
em vez de o modelo inventar uma solução ad-hoc (ver app/schemas/mapping.py).

Cada transformação tem um template por dialeto de SQL E uma função Python
equivalente, para suportar project.etl.engine = "sql" ou "python".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class TransformSpec:
    sql_templates: dict[str, str]          # dialeto -> template com {col}, {params...}
    python_fn: Callable[..., object] | None  # função equivalente para engine="python"
    description: str


def _format_cpf_py(value: str) -> str:
    digits = "".join(c for c in str(value) if c.isdigit())
    return digits.zfill(11)[:11]


def _parse_date_br_py(value: str):
    from datetime import datetime
    return datetime.strptime(value, "%d/%m/%Y").date()


TRANSFORM_REGISTRY: dict[str, TransformSpec] = {
    "regex_extract_digits": TransformSpec(
        sql_templates={
            "postgres": "regexp_replace({col}, '[^0-9]', '', 'g')",
            "sqlserver": "dbo.fn_strip_nondigits({col})",
            "firebird": "fn_strip_nondigits({col})",
        },
        python_fn=_format_cpf_py,
        description="Remove tudo que não é dígito — usado para normalizar CPF/CNPJ.",
    ),
    "parse_date_br": TransformSpec(
        sql_templates={
            "postgres": "to_date({col}, 'DD/MM/YYYY')",
            "sqlserver": "CONVERT(date, {col}, 103)",
            "firebird": "CAST({col} AS DATE)",
        },
        python_fn=_parse_date_br_py,
        description="Converte data em texto DD/MM/AAAA para tipo date.",
    ),
    "map_enum": TransformSpec(
        sql_templates={
            "postgres": "CASE {col} {when_clauses} END",
            "sqlserver": "CASE {col} {when_clauses} END",
            "firebird": "CASE {col} {when_clauses} END",
        },
        python_fn=None,  # construído dinamicamente a partir de params["mapping"]
        description="Traduz um valor de enum legado para o valor esperado no destino.",
    ),
    "split_nome_completo": TransformSpec(
        sql_templates={},  # split de nome é mais simples em Python
        python_fn=lambda value: (value.split(" ", 1)[0], value.split(" ", 1)[-1]),
        description="Separa nome completo em primeiro nome e sobrenome.",
    ),
}


def available_transform_names() -> list[str]:
    return list(TRANSFORM_REGISTRY.keys())


def render_sql(transform_type: str, dialect: str, col: str, params: dict) -> str:
    spec = TRANSFORM_REGISTRY.get(transform_type)
    if spec is None:
        raise ValueError(f"Transformação desconhecida: {transform_type}")
    template = spec.sql_templates.get(dialect)
    if template is None:
        raise ValueError(f"Transformação '{transform_type}' sem template para dialeto '{dialect}'")
    return template.format(col=col, **params)
