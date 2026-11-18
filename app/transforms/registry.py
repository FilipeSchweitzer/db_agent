"""Lista fechada de transformações conhecidas.

O Agente 2 só pode escolher transformações daqui — nunca gera lógica livre.
Se nenhuma entrada servir, o campo é marcado requires_human_review=True
em vez de o modelo inventar uma solução ad-hoc (ver app/schemas/mapping.py).

Cada transformação é uma **expressão de uma coluna só**. A expressão é
escrita uma vez, em SQL canônico (Postgres), e as demais saem de
`sqlglot.transpile(..., read="postgres", write=<dialeto>)`.

Onde a transpilação não serve, a expressão é declarada em `overrides` e o
motivo fica no comentário. A regra é simples: *override só para dialeto cujo
SGBD não tem a função equivalente* — nunca para "eu prefiro assim".

Casos declarados hoje:

- `parse_date_br` no SQL Server: `sqlglot` traduz `TO_DATE` para
  `STR_TO_DATE`, que é MySQL, não T-SQL. T-SQL tem `CONVERT(date, x, 103)`.
- `regex_extract_digits` no SQL Server: T-SQL não tem regex nativa; usa
  `dbo.fn_strip_nondigits`, criada na **origem** (ver `app/etl/generator.py`).
- `regex_extract_digits` no Firebird: **não existe** `REGEXP_REPLACE` no
  Firebird 5 (verificado em servidor real: "Function unknown"), então a
  normalização é uma cadeia de `REPLACE` com os separadores de CPF/CNPJ/RG.
  A Fase 2 tinha exatamente esse bug — o teste de integração é o que impede
  a volta dele.

Firebird não é um dialeto do `sqlglot`: nada chega lá por transpilação. Por
isso `TransformSpec.firebird_sql` é obrigatório — inclusive quando a
expressão é igual à canônica. Assim é impossível uma transformação nova
"vazar" para o Firebird sem alguém ter olhado para ele.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot

SOURCE_DIALECTS = ("firebird", "sqlserver", "postgres")
# Alvos que o sqlglot entende. Firebird fica de fora de propósito: a
# expressão precisa ser declarada à mão em `firebird_sql`.
_TRANSPILE_TARGET = {"sqlserver": "tsql"}
# Dialetos cuja expressão renderizada sabemos validar por parse.
_PARSEABLE = {"postgres": "postgres", "sqlserver": "tsql"}

# Firebird não tem REGEXP_REPLACE; a cadeia cobre os separadores que aparecem
# em CPF/CNPJ/RG ('.', '-', '/', parênteses e espaço).
_FIREBIRD_DIGITS = (
    "REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE({col}, '.', ''), '-', ''), "
    "'/', ''), '(', ''), ')', ''), ' ', '')"
)
# Firebird não tem TO_DATE(col, máscara): monta ISO a partir de DD/MM/AAAA.
_FIREBIRD_DATE = (
    "CAST(SUBSTRING({col} FROM 7 FOR 4) || '-' || SUBSTRING({col} FROM 4 FOR 2) "
    "|| '-' || SUBSTRING({col} FROM 1 FOR 2) AS DATE)"
)


@dataclass(frozen=True)
class TransformSpec:
    canonical_sql: str                      # Postgres — fonte da verdade
    description: str
    # Firebird não passa por transpilação: a expressão é sempre explícita.
    firebird_sql: str | None = None
    # Overrides para dialetos que o sqlglot transpila mas cujo resultado não
    # é executável no SGBD (função inexistente ou de outro dialeto).
    overrides: dict[str, str] = field(default_factory=dict)


def _sql_literal(value: object) -> str:
    """Literal SQL seguro para valores de enum (o único lugar com string livre)."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


TRANSFORM_REGISTRY: dict[str, TransformSpec] = {
    "regex_extract_digits": TransformSpec(
        canonical_sql="regexp_replace({col}, '[^0-9]', '', 'g')",
        firebird_sql=_FIREBIRD_DIGITS,
        overrides={
            "sqlserver": "dbo.fn_strip_nondigits({col})",
        },
        description=(
            "Remove tudo que não é dígito — usado para normalizar CPF/CNPJ. "
            "No SQL Server usa dbo.fn_strip_nondigits, criada na origem; no "
            "Firebird remove os separadores usuais de documento por REPLACE."
        ),
    ),
    "parse_date_br": TransformSpec(
        canonical_sql="to_date({col}, 'DD/MM/YYYY')",
        firebird_sql=_FIREBIRD_DATE,
        overrides={
            "sqlserver": "CONVERT(date, {col}, 103)",
        },
        description="Converte data em texto DD/MM/AAAA para tipo date.",
    ),
    "map_enum": TransformSpec(
        # O CASE é montado a partir de params["mapping"] em `map_enum_sql`;
        # este template só documenta a forma canônica.
        canonical_sql="CASE {col} {when_clauses} END",
        # CASE/WHEN é ANSI: a canônica roda igual no Firebird.
        firebird_sql="CASE {col} {when_clauses} END",
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


def map_enum_sql(col: str, params: dict | None) -> str:
    """CASE canônico; `render_sql` transpila quando o dialeto exige."""
    return f"CASE {col} {when_clauses_from_params(params, col)} END"


def transpile(sql: str, dialect: str) -> str:
    """Traduz uma expressão canônica para um dialeto que o sqlglot conhece.

    Firebird e Postgres não passam por aqui: no primeiro não há dialeto no
    sqlglot, e no segundo a canônica já é o resultado final (reescrevê-la só
    geraria ruído nos artefatos).
    """
    target = _TRANSPILE_TARGET.get(dialect)
    if target is None:
        return sql
    try:
        return sqlglot.transpile(sql, read="postgres", write=target)[0]
    except Exception as exc:  # sqlglot levanta tipos variados
        raise ValueError(
            f"não foi possível transpilar a expressão para '{dialect}': {sql!r} ({exc})"
        ) from exc


def _assert_parses(sql: str, dialect: str) -> str:
    """Valida a expressão com o parser do próprio dialeto de destino.

    Não substitui o teste de integração (semântica e funções nativas não são
    verificadas pelo parser), mas pega o erro mais comum: expressão que não é
    nem sintaticamente válida no SGBD. Firebird não tem parser no sqlglot —
    quem cobre ele é `tests/test_integration_firebird.py`.
    """
    parser = _PARSEABLE.get(dialect)
    if parser is None:
        return sql
    try:
        sqlglot.parse_one(sql, read=parser)
    except Exception as exc:
        raise ValueError(
            f"expressão gerada não é SQL válido em '{dialect}': {sql!r} ({exc})"
        ) from exc
    return sql


def render_sql(transform_type: str, dialect: str, col: str, params: dict | None = None) -> str:
    """Renderiza a expressão SQL de uma transformação para um dialeto de origem."""
    if dialect not in SOURCE_DIALECTS:
        raise ValueError(
            f"Dialeto de origem não suportado: {dialect!r} — "
            f"disponíveis: {', '.join(SOURCE_DIALECTS)}"
        )
    spec = TRANSFORM_REGISTRY.get(transform_type)
    if spec is None:
        raise ValueError(
            f"Transformação desconhecida: {transform_type!r} — "
            f"disponíveis: {', '.join(available_transform_names())}"
        )

    params = params or {}
    if transform_type == "map_enum":
        if dialect == "firebird":
            return _render_template(
                spec.firebird_sql or "", transform_type, col,
                {"when_clauses": when_clauses_from_params(params, col)},
            )
        return _assert_parses(
            transpile(map_enum_sql(col, params), dialect), dialect
        )

    if dialect == "firebird":
        if spec.firebird_sql is None:
            raise ValueError(
                f"Transformação '{transform_type}' não declara expressão para "
                "Firebird (firebird_sql) e o sqlglot não transpila para lá"
            )
        return _render_template(spec.firebird_sql, transform_type, col, params)

    template = spec.overrides.get(dialect)
    if template is not None:
        return _assert_parses(
            _render_template(template, transform_type, col, params), dialect
        )

    canonical = _render_template(spec.canonical_sql, transform_type, col, params)
    return _assert_parses(transpile(canonical, dialect), dialect)


def _render_template(
    template: str, transform_type: str, col: str, params: dict
) -> str:
    try:
        return template.format(col=col, **params)
    except KeyError as exc:
        raise ValueError(
            f"Transformação '{transform_type}' exige o parâmetro {exc.args[0]!r}, "
            f"que não veio em params={params}"
        ) from exc
