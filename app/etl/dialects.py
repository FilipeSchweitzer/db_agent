"""Diferenças de SQL entre dialetos, num só lugar.

Duas responsabilidades:

- **origem** (`quote_source`): como citar identificadores ao ler o legado;
- **destino** (`Dialect`): DDL, sequence, insert idempotente e transação.

Nenhum outro módulo escreve SQL específico de dialeto — é aqui que nasce
qualquer divergência, e é aqui que ela deve ser corrigida.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.schemas.target_schema import TargetField

SOURCE_DIALECTS = ("firebird", "sqlserver", "postgres")
TARGET_DIALECTS = ("postgres", "sqlserver", "firebird")

# Largura/ escala padrão quando o YAML não informa (`string` -> VARCHAR(255)).
_DEFAULT_STRING_WIDTH = 255
_DEFAULT_DECIMAL = (18, 2)


def _parse_type(type_spec: str | None) -> tuple[str, int | None, int | None]:
    pattern = r"\s*([A-Za-z_]+)\s*(?:\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\))?\s*"
    match = re.fullmatch(pattern, type_spec or "")
    if not match:
        raise ValueError(f"tipo inválido no target schema: {type_spec!r}")
    base = match.group(1).lower()
    width = int(match.group(2)) if match.group(2) else None
    scale = int(match.group(3)) if match.group(3) else None
    return base, width, scale


def _single_line(text: str) -> str:
    """Achata SQL multilinha para caber dentro de um EXECUTE STATEMENT.

    Firebird não tem `CREATE TABLE IF NOT EXISTS`; o jeito de tornar o script
    reexecutável é montar o DDL como string dentro de um `EXECUTE BLOCK`, e
    para isso o corpo precisa estar em uma linha só.
    """
    return " ".join(line.strip() for line in text.splitlines() if line.strip())


# ----------------------------------------------------------------------
# Origem (legado): só quoting
# ----------------------------------------------------------------------
_SOURCE_QUOTE = {
    "firebird": ('"', '"'),
    "postgres": ('"', '"'),
    "sqlserver": ("[", "]"),
}


def quote_source(dialect: str, name: str) -> str:
    """Cita um identificador do banco legado.

    Nomes de legado (Firebird) costumam vir em maiúsculas e com caracteres
    fora do padrão; citar sempre é a única forma de não depender de folding
    de caixa do SGBD.
    """
    if dialect not in _SOURCE_QUOTE:
        raise ValueError(f"Dialeto de origem não suportado: {dialect}")
    if not name or '"' in name or "]" in name:
        raise ValueError(f"Identificador inválido na origem: {name!r}")
    open_quote, close_quote = _SOURCE_QUOTE[dialect]
    return f"{open_quote}{name}{close_quote}"


def source_column(dialect: str, table_alias: str, column: str) -> str:
    return f"{table_alias}.{quote_source(dialect, column)}"


# ----------------------------------------------------------------------
# Destino
# ----------------------------------------------------------------------
def _postgres_type(field: TargetField) -> str:
    base, width, scale = _parse_type(field.type)
    if base in ("integer", "int", "bigint", "serial", "bigserial"):
        return "BIGINT"
    if base == "smallint":
        return "SMALLINT"
    if base == "string":
        return f"VARCHAR({width or _DEFAULT_STRING_WIDTH})" if width else "TEXT"
    if base in ("date",):
        return "DATE"
    if base in ("timestamp", "datetime"):
        return "TIMESTAMP"
    if base in ("decimal", "numeric", "money"):
        w, s = (width or _DEFAULT_DECIMAL[0]), (scale or _DEFAULT_DECIMAL[1])
        return f"NUMERIC({w},{s})"
    if base == "boolean":
        return "BOOLEAN"
    if base == "uuid":
        return "UUID"
    raise ValueError(f"tipo '{field.type}' sem mapeamento para postgres")


def _sqlserver_type(field: TargetField) -> str:
    base, width, scale = _parse_type(field.type)
    if base in ("integer", "int", "bigint", "serial", "bigserial"):
        return "BIGINT"
    if base == "smallint":
        return "SMALLINT"
    if base == "string":
        return f"NVARCHAR({width or _DEFAULT_STRING_WIDTH})"
    if base == "date":
        return "DATE"
    if base in ("timestamp", "datetime"):
        return "DATETIME2"
    if base in ("decimal", "numeric", "money"):
        w, s = (width or _DEFAULT_DECIMAL[0]), (scale or _DEFAULT_DECIMAL[1])
        return f"DECIMAL({w},{s})"
    if base == "boolean":
        return "BIT"
    if base == "uuid":
        return "UNIQUEIDENTIFIER"
    raise ValueError(f"tipo '{field.type}' sem mapeamento para sqlserver")


def _firebird_type(field: TargetField) -> str:
    base, width, scale = _parse_type(field.type)
    if base in ("integer", "int", "bigint", "serial", "bigserial"):
        return "BIGINT"
    if base == "smallint":
        return "SMALLINT"
    if base == "string":
        return f"VARCHAR({width or _DEFAULT_STRING_WIDTH})"
    if base == "date":
        return "DATE"
    if base in ("timestamp", "datetime"):
        return "TIMESTAMP"
    if base in ("decimal", "numeric", "money"):
        w, s = (width or _DEFAULT_DECIMAL[0]), (scale or _DEFAULT_DECIMAL[1])
        return f"NUMERIC({w},{s})"
    if base == "boolean":
        return "SMALLINT"
    if base == "uuid":
        return "CHAR(36)"
    raise ValueError(f"tipo '{field.type}' sem mapeamento para firebird")


_TYPE_RENDERERS = {
    "postgres": _postgres_type,
    "sqlserver": _sqlserver_type,
    "firebird": _firebird_type,
}


@dataclass(frozen=True)
class Dialect:
    name: str

    # ---------------- identificadores ----------------
    def quote(self, name: str) -> str:
        if self.name == "sqlserver":
            return f"[{name}]"
        return f'"{name}"'

    def qualified(self, schema: str | None, table: str) -> str:
        return f"{self.quote(schema)}.{self.quote(table)}" if schema else self.quote(table)

    # ---------------- tipos ----------------
    def column_type(self, field: TargetField) -> str:
        renderer = _TYPE_RENDERERS.get(self.name)
        if renderer is None:
            raise ValueError(f"Dialeto de destino não suportado: {self.name}")
        return renderer(field)

    # ---------------- sequences ----------------
    def next_value(self, schema: str | None, sequence: str) -> str:
        ref = self.qualified(schema, sequence)
        if self.name == "postgres":
            return f"nextval('{schema}.{sequence}')" if schema else f"nextval('{sequence}')"
        return f"NEXT VALUE FOR {ref}"

    def create_sequence(self, schema: str | None, sequence: str) -> list[str]:
        ref = self.qualified(schema, sequence)
        if self.name == "postgres":
            return [f"CREATE SEQUENCE IF NOT EXISTS {ref} START WITH 1 INCREMENT BY 1;"]
        if self.name == "sqlserver":
            return [
                f"IF OBJECT_ID('{schema + '.' if schema else ''}{sequence}', 'SO') IS NULL",
                f"    EXEC('CREATE SEQUENCE {schema + '.' if schema else ''}{sequence} "
                "AS BIGINT START WITH 1 INCREMENT BY 1');",
            ]
        # Firebird não tem IF NOT EXISTS: o guard vive num EXECUTE BLOCK.
        return [
            "EXECUTE BLOCK AS BEGIN",
            "  IF (NOT EXISTS(SELECT 1 FROM RDB$GENERATORS "
            f"WHERE RDB$GENERATOR_NAME = '{sequence}')) THEN",
            f"    EXECUTE STATEMENT 'CREATE SEQUENCE {ref}';",
            "END;",
        ]

    # ---------------- DDL ----------------
    def create_table(
        self,
        schema: str | None,
        table: str,
        column_sql: list[str],
        constraint_sql: list[str],
    ) -> list[str]:
        ref = self.qualified(schema, table)
        body = ",\n".join(f"    {line}" for line in [*column_sql, *constraint_sql])
        if self.name == "sqlserver":
            return [
                f"IF OBJECT_ID('{schema + '.' if schema else ''}{table}', 'U') IS NULL",
                f"CREATE TABLE {ref} (\n{body}\n);",
            ]
        if self.name == "postgres":
            return [f"CREATE TABLE IF NOT EXISTS {ref} (\n{body}\n);"]
        flat_body = _single_line(body).replace("'", "''")
        return [
            "EXECUTE BLOCK AS BEGIN",
            "  IF (NOT EXISTS(SELECT 1 FROM RDB$RELATIONS "
            f"WHERE RDB$RELATION_NAME = '{table}')) THEN",
            f"    EXECUTE STATEMENT 'CREATE TABLE {ref} ({flat_body})';",
            "END;",
        ]

    def primary_key_constraint(self, table: str, column: str) -> str:
        name = self.constraint_name("pk", table, column)
        return f"CONSTRAINT {name} PRIMARY KEY ({self.quote(column)})"

    def foreign_key_constraint(
        self,
        table: str,
        column: str,
        ref_table: str,
        ref_column: str,
        schema: str | None,
    ) -> str:
        name = self.constraint_name("fk", table, column)
        return (
            f"CONSTRAINT {name} FOREIGN KEY ({self.quote(column)}) "
            f"REFERENCES {self.qualified(schema, ref_table)} ({self.quote(ref_column)})"
        )

    def sequence_default(self, schema: str | None, sequence: str) -> str:
        return f"DEFAULT {self.next_value(schema, sequence)}"

    # ---------------- staging ----------------
    def create_staging_table(
        self, schema: str | None, name: str, old_key_type: str
    ) -> list[str]:
        """Tabela de-para: chave antiga -> chave nova. Reexecutável."""
        ref = self.qualified(schema, name)
        constraint = "pk_" + name
        body = (
            f"    {self.quote('old_key')} {old_key_type},\n"
            f"    {self.quote('new_id')} BIGINT,\n"
            f"    CONSTRAINT {constraint} PRIMARY KEY ({self.quote('old_key')})"
        )
        if self.name == "postgres":
            return [f"CREATE TABLE IF NOT EXISTS {ref} (\n{body}\n);"]
        if self.name == "sqlserver":
            return [
                f"IF OBJECT_ID('{schema + '.' if schema else ''}{name}', 'U') IS NULL",
                f"CREATE TABLE {ref} (\n{body}\n);",
            ]
        # Identificador citado é case-sensitive no Firebird: conferir pelo
        # nome exato, não pela versão em maiúsculas.
        flat_body = " ".join(line.strip() for line in body.splitlines())
        return [
            "EXECUTE BLOCK AS BEGIN",
            "  IF (NOT EXISTS(SELECT 1 FROM RDB$RELATIONS "
            f"WHERE RDB$RELATION_NAME = '{name}')) THEN",
            f"    EXECUTE STATEMENT 'CREATE TABLE {ref} ({flat_body})';",
            "END;",
        ]

    # ---------------- transação ----------------
    def begin(self) -> str | None:
        if self.name == "postgres":
            return "BEGIN;"
        if self.name == "sqlserver":
            return "BEGIN TRANSACTION;"
        return None  # Firebird: autocommit DDL + COMMIT explícito ao final

    def commit(self) -> str:
        return "COMMIT;"

    # ---------------- insert ----------------
    def insert_header(
        self, schema: str | None, table: str, columns: list[str], idempotent: bool
    ) -> str:
        cols = ", ".join(self.quote(column) for column in columns)
        firebird_upsert = idempotent and self.name == "firebird"
        verb = "UPDATE OR INSERT INTO" if firebird_upsert else "INSERT INTO"
        return f"{verb} {self.qualified(schema, table)} ({cols})"

    def idempotent_filter(self, target_ref: str, pk: str, pk_expr: str) -> str | None:
        """Filtro `WHERE NOT EXISTS` para SQL Server (não tem ON CONFLICT)."""
        if self.name != "sqlserver":
            return None
        return (
            f"NOT EXISTS (SELECT 1 FROM {target_ref} t WHERE t.{self.quote(pk)} = {pk_expr})"
        )

    def idempotent_suffix(self) -> str | None:
        if self.name == "postgres":
            return "ON CONFLICT DO NOTHING"
        return None

    # ---------------- constraints ----------------
    def add_constraint(
        self,
        schema: str | None,
        table: str,
        name: str,
        constraint_sql: str,
    ) -> list[str]:
        """ALTER TABLE protegido: rodar o arquivo duas vezes não falha."""
        target = self.qualified(schema, table)
        alter = f"ALTER TABLE {target} ADD {constraint_sql};"
        if self.name == "postgres":
            return [
                "DO $$",
                "BEGIN",
                f"    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}') THEN",
                f"        {alter}",
                "    END IF;",
                "END $$;",
            ]
        if self.name == "sqlserver":
            return [
                f"IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = '{name}')",
                f"    {alter}",
            ]
        return [
            "EXECUTE BLOCK AS BEGIN",
            "  IF (NOT EXISTS(SELECT 1 FROM RDB$RELATION_CONSTRAINTS "
            f"WHERE RDB$CONSTRAINT_NAME = '{name.upper()}')) THEN",
            f"    EXECUTE STATEMENT '{alter}';",
            "END;",
        ]

    @staticmethod
    def constraint_name(prefix: str, table: str, column: str) -> str:
        # Nomes de constraint são limitados (Firebird: 31 chars) e as tabelas
        # do legado podem gerar nomes enormes — truncar mantém o script válido.
        return f"{prefix}_{table}_{column}"[:28]


def get_dialect(name: str) -> Dialect:
    if name not in TARGET_DIALECTS:
        raise ValueError(f"Dialeto de destino não suportado: {name}")
    return Dialect(name)
