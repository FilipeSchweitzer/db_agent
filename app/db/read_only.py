"""Allowlist de leitura única, compartilhada por todas as tools.

A validação é feita **parseando** o SQL, não procurando palavras com regex:
regex erra nas duas direções (bloqueia `updated_at` porque contém `update` e
deixa passar `MERGE`, `GRANT`, `CALL`, `COPY` e múltiplos statements).

Aqui existe uma diferença importante de responsabilidade:

- esta allowlist é **defesa em profundidade** — impede o caminho óbvio e dá
  erro claro cedo;
- a fronteira de segurança de verdade é o **GRANT**: o usuário de banco usado
  para ler o legado deve ter apenas `SELECT` nas tabelas do escopo (ver
  README, "Provisionamento do usuário de leitura").

Nenhuma das duas substitui a outra: um bug de parse não deve virar escrita no
legado, e um GRANT amplo não deve virar escrita por acidente.
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp


class ReadOnlyViolation(ValueError):
    """SQL recusado porque não é uma única consulta de leitura."""


# Dialeto de parse por dialeto de conexão do SQLAlchemy. Firebird não tem
# dialeto no sqlglot; o parser padrão aceita o subconjunto que a introspecção
# usa (SELECT/WITH, `FETCH FIRST`, identificadores com `$`).
_PARSER = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "sqlserver": "tsql",
    "mssql": "tsql",
}

# `SELECT ... INTO tabela` é sintaticamente um SELECT e escreve: o sqlglot
# expõe isso como `into`, então a checagem de tipo sozinha não bastaria.
_FORBIDDEN_NODES = (
    exp.Into,
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
)


def _parser_for(dialect: str | None) -> str | None:
    if not dialect:
        return None
    return _PARSER.get(dialect.split(".")[0].lower())


def ensure_read_only(sql: str, dialect: str | None = None) -> str:
    """Devolve o SQL original se for exatamente uma consulta; senão, levanta.

    `dialect` é o nome do dialeto do SQLAlchemy (`postgresql`, `mssql`,
    `firebird`); quando desconhecido, usa o parser genérico do sqlglot.
    """
    if not sql or not sql.strip():
        raise ReadOnlyViolation("Query vazia: apenas uma consulta SELECT é permitida.")

    try:
        statements = sqlglot.parse(sql, read=_parser_for(dialect))
    except Exception as exc:
        raise ReadOnlyViolation(
            f"SQL inválido ou não suportado — apenas uma consulta SELECT é permitida ({exc})"
        ) from exc

    statements = [stmt for stmt in statements if stmt is not None]
    if len(statements) != 1:
        raise ReadOnlyViolation(
            f"Exatamente uma consulta é permitida por chamada; recebi {len(statements)}."
        )

    statement = statements[0]
    if not isinstance(statement, exp.Query):
        raise ReadOnlyViolation(
            f"Statement '{type(statement).__name__}' bloqueado: "
            "apenas SELECT/WITH são permitidos nesta tool."
        )

    for node in statement.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise ReadOnlyViolation(
                f"Cláusula '{type(node).__name__}' bloqueada: "
                "apenas SELECT/WITH são permitidos nesta tool."
            )

    return sql
