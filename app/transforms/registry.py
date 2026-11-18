"""Lista fechada de transformações conhecidas.

O Agente 2 só pode escolher transformações daqui — nunca gera lógica livre.
Se nenhuma entrada servir, o campo é marcado requires_human_review=True
em vez de o modelo inventar uma solução ad-hoc (ver app/schemas/mapping.py).

Cada transformação é uma **expressão de uma coluna só**, com template por
dialeto. Isso é uma restrição deliberada: transformações que produzem duas
colunas (separar nome em primeiro nome + sobrenome) não cabem no contrato
`entrada -> uma coluna de destino` e ficam de fora até existir suporte a
múltiplos alvos.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TransformSpec:
    sql_templates: dict[str, str]          # dialeto -> template com {col}, {params...}
    description: str


def _sql_literal(value: object) -> str:
    """Literal SQL seguro para valores de enum (o único lugar com string livre)."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def render_map_enum(when_clauses: str) -> str:
    return f"CASE {{col}} {when_clauses} END"


TRANSFORM_REGISTRY: dict[str, TransformSpec] = {
    "regex_extract_digits": TransformSpec(
        sql_templates={
            "postgres": "regexp_replace({col}, '[^0-9]', '', 'g')",
            "sqlserver": "dbo.fn_strip_nondigits({col})",
            # Firebird 4+ tem REGEXP_REPLACE nativo — não exige UDF externa.
            "firebird": "REGEXP_REPLACE({col}, '[^0-9]', '')",
        },
        description=(
            "Remove tudo que não é dígito — usado para normalizar CPF/CNPJ. "
            "No SQL Server usa dbo.fn_strip_nondigits, criada por etl_schema.sql; "
            "no Firebird exige versão 4+ (REGEXP_REPLACE)."
        ),
    ),
    "parse_date_br": TransformSpec(
        sql_templates={
            "postgres": "to_date({col}, 'DD/MM/YYYY')",
            "sqlserver": "CONVERT(date, {col}, 103)",
            "firebird": "CAST({col} AS DATE)",
        },
        description="Converte data em texto DD/MM/AAAA para tipo date.",
    ),
    "map_enum": TransformSpec(
        sql_templates={
            "postgres": "CASE {col} {when_clauses} END",
            "sqlserver": "CASE {col} {when_clauses} END",
            "firebird": "CASE {col} {when_clauses} END",
        },
        description=(
            "Traduz valor de enum legado. params obrigatório: "
            '{"mapping": {"valor_legado": "valor_novo"}}.'
        ),
    ),
}


def available_transform_names() -> list[str]:
    return list(TRANSFORM_REGISTRY.keys())


def transform_descriptions() -> dict[str, str]:
    """Descrição por transformação, usada no prompt do Agente 2."""
    return {name: spec.description for name, spec in TRANSFORM_REGISTRY.items()}


def when_clauses_from_params(params: dict | None, col: str) -> str:
    """Monta `WHEN 'a' THEN 'b' ... ELSE {col}` a partir de params["mapping"].

    Chave ausente no mapa mantém o valor original (`ELSE <col>`) em vez de
    virar NULL silencioso — dado desconhecido é melhor que dado perdido, e o
    relatório de validação sinaliza o que não foi traduzido.
    """
    mapping = (params or {}).get("mapping")
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError(
            "transformação 'map_enum' exige params={'mapping': {valor_legado: valor_novo}}"
        )
    clauses = " ".join(
        f"WHEN {_sql_literal(old)} THEN {_sql_literal(new)}" for old, new in mapping.items()
    )
    return f"{clauses} ELSE {col}"


def render_sql(transform_type: str, dialect: str, col: str, params: dict | None = None) -> str:
    """Renderiza a expressão SQL de uma transformação para um dialeto de origem."""
    spec = TRANSFORM_REGISTRY.get(transform_type)
    if spec is None:
        raise ValueError(
            f"Transformação desconhecida: {transform_type!r} — "
            f"disponíveis: {', '.join(available_transform_names())}"
        )
    template = spec.sql_templates.get(dialect)
    if template is None:
        raise ValueError(
            f"Transformação '{transform_type}' sem template para o dialeto '{dialect}'"
        )

    params = params or {}
    if transform_type == "map_enum":
        return f"CASE {col} {when_clauses_from_params(params, col)} END"

    try:
        return template.format(col=col, **params)
    except KeyError as exc:
        raise ValueError(
            f"Transformação '{transform_type}' exige o parâmetro {exc.args[0]!r}, "
            f"que não veio em params={params}"
        ) from exc
