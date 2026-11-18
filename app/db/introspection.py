"""Tools que o Agente 1 usa para investigar o banco legado.

Cada função aqui é uma "tool" no sentido de function-calling: o LLM decide
quando chamar cada uma, mas a execução real é código determinístico —
o modelo nunca roda SQL arbitrário sem passar por run_readonly_query,
que é a única tool com uma validação extra de segurança.
"""
from __future__ import annotations

import re

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

# Regras simples de detecção de PII por nome de coluna — ponto de partida,
# o Agente 1 deve refinar isso olhando os valores reais, não só o nome.
_PII_NAME_HINTS = re.compile(
    r"cpf|cnpj|rg\b|nome|endereco|telefone|email|nascimento|matricula",
    re.IGNORECASE,
)


def list_tables(engine: Engine) -> list[str]:
    return inspect(engine).get_table_names()


def get_schema(engine: Engine, table: str) -> list[dict]:
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
    with engine.connect() as conn:
        # NOTA: nomes de tabela não são parametrizáveis via bind — validar
        # contra list_tables() antes de chamar esta função.
        result = conn.execute(text(f"SELECT * FROM {table} FETCH FIRST {n} ROWS ONLY"))
        return [dict(row._mapping) for row in result]


def profile_column(engine: Engine, table: str, column: str) -> dict:
    with engine.connect() as conn:
        total = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
        nulls = conn.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE {column} IS NULL")
        ).scalar()
        distinct = conn.execute(
            text(f"SELECT COUNT(DISTINCT {column}) FROM {table}")
        ).scalar()
        top_values = conn.execute(
            text(
                f"SELECT {column}, COUNT(*) AS n FROM {table} "
                f"WHERE {column} IS NOT NULL GROUP BY {column} "
                f"ORDER BY n DESC FETCH FIRST 10 ROWS ONLY"
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
    forbidden = re.compile(r"\b(insert|update|delete|drop|alter|truncate)\b", re.IGNORECASE)
    if forbidden.search(sql):
        raise ValueError("Query bloqueada: apenas leitura é permitida nesta tool.")
    with engine.connect() as conn:
        result = conn.execute(text(sql))
        return [dict(row._mapping) for row in result]
