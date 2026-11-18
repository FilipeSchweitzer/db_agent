"""Tools que o Agente 1 usa para investigar o banco legado.

Cada função aqui é uma "tool" no sentido de function-calling: o LLM decide
quando chamar cada uma, mas a execução real é código determinístico —
o modelo nunca roda SQL arbitrário sem passar por `run_readonly_query`,
que valida o statement por parse (ver `app/db/read_only.py`).

Duas regras que valem para todas as tools:

1. **Identificador sempre citado, pelo dialeto da conexão.** Nome de legado
   tem maiúscula, espaço e caractere especial; e `[PESSOA]` (T-SQL) não é o
   mesmo que `"PESSOA"`. Quem sabe citar é o `identifier_preparer` do
   SQLAlchemy — não vale escrever aspas à mão.
2. **Paginação por dialeto.** `FETCH FIRST` não existe no T-SQL; `TOP` não
   existe no Postgres. O SQL montado aqui é o mínimo portável; o resto é
   responsabilidade do driver.
"""
from __future__ import annotations

import re

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.db.read_only import ensure_read_only

# Regras simples de detecção de PII por nome de coluna — ponto de partida,
# o Agente 1 deve refinar isso olhando os valores reais, não só o nome.
_PII_NAME_HINTS = re.compile(
    r"cpf|cnpj|rg\b|nome|endereco|telefone|email|nascimento|matricula",
    re.IGNORECASE,
)


def _dialect_name(engine: Engine) -> str:
    """`firebird.firebird` -> `firebird`; `mssql` -> `mssql`."""
    return engine.dialect.name.split(".")[0].lower()


def _quote(engine: Engine, name: str) -> str:
    return engine.dialect.identifier_preparer.quote(name)


def _resolve(engine: Engine, table: str) -> str:
    """Resolve o nome real da tabela, sem depender de caixa.

    O Firebird devolve os nomes em maiúsculas; o agente costuma falar em
    minúsculas. Comparar sem caixa e usar o nome **real** na query mantém a
    tool tolerante sem abrir espaço para nome arbitrário: o que não existe
    em `list_tables()` é recusado.
    """
    known = {name.lower(): name for name in list_tables(engine)}
    actual = known.get(table.lower())
    if actual is None:
        raise ValueError(
            f"Tabela {table!r} não existe no escopo inspecionado "
            f"({len(known)} tabelas conhecidas)."
        )
    return actual


def _resolve_column(engine: Engine, table: str, column: str) -> str:
    known = {col["name"].lower(): col["name"] for col in get_schema(engine, table)}
    actual = known.get(column.lower())
    if actual is None:
        raise ValueError(
            f"Coluna {column!r} não existe em {table!r} "
            f"(conhecidas: {', '.join(sorted(known.values()))})."
        )
    return actual


def _limit_sql(engine: Engine, n: int) -> tuple[str, str]:
    """Devolve (prefixo, sufixo) da paginação para o dialeto da conexão.

    T-SQL usa `TOP`, que vem **antes** das colunas; Postgres usa `LIMIT` e
    Firebird usa `FETCH FIRST ... ROWS ONLY`, ambos no fim.
    """
    n = int(n)
    if n < 1:
        raise ValueError(f"Limite inválido: {n}")
    dialect = _dialect_name(engine)
    if dialect == "mssql":
        return f"TOP ({n}) ", ""
    if dialect == "postgres":
        return "", f" LIMIT {n}"
    return "", f" FETCH FIRST {n} ROWS ONLY"


def list_tables(engine: Engine) -> list[str]:
    return inspect(engine).get_table_names()


def get_schema(engine: Engine, table: str) -> list[dict]:
    table = _resolve(engine, table)
    insp = inspect(engine)
    columns = insp.get_columns(table)
    pk = set(insp.get_pk_constraint(table).get("constrained_columns", []))
    fks = insp.get_foreign_keys(table)
    return [
        {
            "name": col["name"],
            "type": str(col["type"]),
            "nullable": col.get("nullable", True),
            "primary_key": col["name"] in pk,
            "foreign_keys": [fk for fk in fks if col["name"] in fk.get("constrained_columns", [])],
        }
        for col in columns
    ]


def sample_rows(engine: Engine, table: str, n: int = 20) -> list[dict]:
    table = _resolve(engine, table)
    prefix, suffix = _limit_sql(engine, n)
    sql = f"SELECT {prefix}* FROM {_quote(engine, table)}{suffix}"
    with engine.connect() as conn:
        result = conn.execute(text(sql))
        return [dict(row._mapping) for row in result]


def profile_column(engine: Engine, table: str, column: str) -> dict:
    table = _resolve(engine, table)
    column = _resolve_column(engine, table, column)
    table_ref = _quote(engine, table)
    column_ref = _quote(engine, column)
    prefix, suffix = _limit_sql(engine, 10)

    with engine.connect() as conn:
        total = conn.execute(text(f"SELECT COUNT(*) FROM {table_ref}")).scalar()
        nulls = conn.execute(
            text(f"SELECT COUNT(*) FROM {table_ref} WHERE {column_ref} IS NULL")
        ).scalar()
        distinct = conn.execute(
            text(f"SELECT COUNT(DISTINCT {column_ref}) FROM {table_ref}")
        ).scalar()
        top_values = conn.execute(
            text(
                f"SELECT {prefix}{column_ref}, COUNT(*) AS n FROM {table_ref} "
                f"WHERE {column_ref} IS NOT NULL GROUP BY {column_ref} "
                f"ORDER BY n DESC{suffix}"
            )
        ).fetchall()

    return {
        "table": table,
        "column": column,
        "row_count": total,
        "null_rate": (nulls / total) if total else 0.0,
        "distinct_count": distinct,
        "top_values": [str(r[0]) for r in top_values],
    }


def detect_pii_by_name(column_name: str) -> bool:
    """Heurística rápida por nome — o Agente 1 deve confirmar com profile_column."""
    return bool(_PII_NAME_HINTS.search(column_name))


def run_readonly_query(engine: Engine, sql: str) -> list[dict]:
    """Executa SQL livre **apenas** se for uma consulta de leitura.

    A allowlist é por parse (`app/db/read_only.py`), não por regex; e o
    usuário de banco da origem deve ter só `GRANT SELECT` — as duas camadas
    são necessárias e nenhuma substitui a outra.
    """
    ensure_read_only(sql, _dialect_name(engine))
    with engine.connect() as conn:
        result = conn.execute(text(sql))
        return [dict(row._mapping) for row in result]
