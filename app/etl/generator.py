"""Gera os artefatos de migração a partir de um `MigrationPlan`.

Quatro arquivos, cada um com um momento de execução:

| arquivo | quando rodar |
|---|---|
| `etl_schema.sql` | antes da primeira carga (sequences + tabelas) |
| `etl_script.sql` | a carga, em ordem de dependência, um COMMIT por tabela |
| `etl_constraints.sql` | depois da carga (FKs, para não validar linha a linha) |
| `etl_validation.sql` | depois da carga — contagens e órfãos de FK |

Nada aqui decide o que migrar: as decisões estão no plano. Este módulo só
traduz o plano em SQL, dialeto por dialeto.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.etl.dialects import Dialect, get_dialect, quote_source
from app.etl.plan import OWN_MAP_ALIAS, FkStep, MigrationPlan, TableStep
from app.schemas.mapping import MappingDocument
from app.schemas.target_schema import TargetField, TargetSchema


@dataclass
class ETLArtifacts:
    schema_sql: str
    load_sql: str
    constraints_sql: str
    validation_sql: str
    report_md: str
    plan: dict

    def files(self) -> dict[str, str]:
        return {
            "etl_schema.sql": self.schema_sql,
            "etl_script.sql": self.load_sql,
            "etl_constraints.sql": self.constraints_sql,
            "etl_validation.sql": self.validation_sql,
            "relatorio.md": self.report_md,
        }


def generate(
    plan: MigrationPlan,
    target_schema: TargetSchema,
    mapping: MappingDocument,
) -> ETLArtifacts:
    return ETLArtifacts(
        schema_sql=render_schema_sql(plan, target_schema),
        load_sql=render_load_sql(plan),
        constraints_sql=render_constraints_sql(plan, target_schema),
        validation_sql=render_validation_sql(plan),
        report_md=render_report(plan, mapping),
        plan=plan.to_artifact(),
    )


# ----------------------------------------------------------------------
# Schema
# ----------------------------------------------------------------------
SQLSERVER_DIGITS_HELPER = """CREATE OR ALTER FUNCTION dbo.fn_strip_nondigits (@value NVARCHAR(MAX))
RETURNS NVARCHAR(MAX)
AS
BEGIN
    DECLARE @i INT = 1, @out NVARCHAR(MAX) = '';
    WHILE @i <= LEN(@value)
    BEGIN
        IF SUBSTRING(@value, @i, 1) LIKE '[0-9]'
            SET @out = @out + SUBSTRING(@value, @i, 1);
        SET @i = @i + 1;
    END
    RETURN @out;
END"""


def prerequisites(plan: MigrationPlan) -> list[str]:
    """Dependências que o script não pode criar sozinho, com o motivo.

    `regex_extract_digits` no SQL Server chama uma função que precisa existir
    no banco de ORIGEM (é lá que a expressão é avaliada). O script roda no
    destino e não tem como criar objeto na origem — então o pré-requisito é
    declarado, não escondido.
    """
    used = {
        column.source_expr
        for step in plan.steps
        for column in step.columns
    }
    if plan.source_dialect == "sqlserver" and any(
        "fn_strip_nondigits(" in expr for expr in used
    ):
        return [
            "SQL Server: a origem precisa de dbo.fn_strip_nondigits "
            "(usada por regex_extract_digits):\n" + SQLSERVER_DIGITS_HELPER,
        ]
    return []


def render_schema_sql(plan: MigrationPlan, target_schema: TargetSchema) -> str:
    dialect = get_dialect(plan.target_dialect)
    lines = [
        f"-- Schema de destino (target_schema versão {plan.target_schema_version})",
        f"-- Projeto: {plan.project} — mapeamento {plan.mapping_id}",
        "-- Rode este arquivo ANTES de etl_script.sql.",
        "",
    ]

    if not plan.create_schema:
        # O schema de destino é gerenciado por outro time: criar tabela aqui
        # divergiria do que está em produção. O script declara o que assume.
        lines.extend(
            [
                "-- etl.create_schema = false: as tabelas e sequences do destino",
                "-- são gerenciadas fora deste pipeline. Este arquivo está vazio",
                "-- de propósito — o que a carga assume:",
            ]
        )
        for step in plan.steps:
            if step.sequence:
                lines.append(
                    f"--   {dialect.qualified(plan.target_schema_name, step.sequence)}"
                )
            lines.append(f"--   {dialect.qualified(plan.target_schema_name, step.table)}")
        return "\n".join(lines).rstrip() + "\n"

    for step in plan.steps:
        table = target_schema.tables[step.table]
        if step.sequence:
            lines.append(f"-- sequence de {step.table}")
            lines.extend(dialect.create_sequence(plan.target_schema_name, step.sequence))
            lines.append("")

        column_sql: list[str] = []
        for name, field in table.fields.items():
            column_sql.append(_column_definition(dialect, plan, step, name, field))

        constraints = (
            [dialect.primary_key_constraint(step.table, step.pk)] if step.pk else []
        )
        lines.extend(
            dialect.create_table(
                plan.target_schema_name, step.table, column_sql, constraints
            )
        )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _column_definition(
    dialect: Dialect,
    plan: MigrationPlan,
    step: TableStep,
    name: str,
    field: TargetField,
) -> str:
    parts = [dialect.quote(name), dialect.column_type(field)]
    if field.primary_key and step.sequence:
        parts.append(dialect.sequence_default(plan.target_schema_name, step.sequence))
    if field.required or field.primary_key:
        parts.append("NOT NULL")
    return " ".join(parts)


# ----------------------------------------------------------------------
# Carga
# ----------------------------------------------------------------------
def render_load_sql(plan: MigrationPlan) -> str:
    dialect = get_dialect(plan.target_dialect)
    lines = [
        f"-- Carga de dados — projeto {plan.project}, mapeamento {plan.mapping_id}",
        f"-- Ordem de dependência: {' -> '.join(step.table for step in plan.steps)}",
        (
            "-- Reexecutável: tabela de-para preserva a chave nova e cada INSERT é"
            " idempotente."
            if plan.idempotent
            else "-- ATENÇÃO: idempotência desligada — rodar duas vezes duplica os dados."
        ),
        "",
    ]
    for prerequisite in prerequisites(plan):
        lines.append("-- PRÉ-REQUISITO NA ORIGEM (não é executado por este arquivo):")
        lines.extend(f"--   {line}" for line in prerequisite.splitlines())
        lines.append("")

    for position, step in enumerate(plan.steps, start=1):
        begin = dialect.begin()
        if begin:
            lines.append(begin)
        lines.append(f"-- {position}. {step.table} <- {step.source_table}")

        if step.sequence and step.map_table:
            lines.extend(_render_staging(plan, dialect, step))
            lines.append("")

        if step.estimated_rows is not None:
            lines.append(f"--    estimativa: {step.estimated_rows} linha(s)")
        lines.extend(_render_insert(plan, dialect, step))
        lines.append(dialect.commit())
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _render_staging(plan: MigrationPlan, dialect: Dialect, step: TableStep) -> list[str]:
    assert step.map_table and step.sequence and step.old_key_expr
    map_ref = dialect.qualified(plan.target_schema_name, step.map_table)
    source_ref = _source_ref(plan, step.source_table)
    old_key = step.old_key_expr
    old_key_type = dialect.column_type(TargetField(type=step.old_key_type))

    lines = [
        f"-- de-para {step.map_table}: chave antiga -> nova chave de {step.table}",
        *dialect.create_staging_table(plan.target_schema_name, step.map_table, old_key_type),
    ]
    if dialect.name == "firebird":
        # Firebird não aceita DDL e DML na mesma transação: o CREATE acima
        # precisa ser confirmado antes do INSERT.
        lines.append(dialect.commit())
    lines.extend(
        [
            f"INSERT INTO {map_ref} ({dialect.quote('old_key')}, {dialect.quote('new_id')})",
            f"SELECT {old_key}, {dialect.next_value(plan.target_schema_name, step.sequence)}",
            f"FROM {source_ref} s",
            f"WHERE {old_key} IS NOT NULL",
            (
                f"  AND NOT EXISTS (SELECT 1 FROM {map_ref} m "
                f"WHERE m.{dialect.quote('old_key')} = {old_key});"
            ),
        ]
    )
    return lines


def _render_insert(plan: MigrationPlan, dialect: Dialect, step: TableStep) -> list[str]:
    target_ref = dialect.qualified(plan.target_schema_name, step.table)
    source_ref = _source_ref(plan, step.source_table)
    columns = [column.target_field for column in step.columns]
    select_list = ",\n".join(
        f"  {column.source_expr} AS {dialect.quote(column.target_field)}"
        for column in step.columns
    )

    lines = [dialect.insert_header(plan.target_schema_name, step.table, columns, plan.idempotent)]
    lines.append("SELECT")
    lines.append(select_list)
    lines.append(f"FROM {source_ref} s")

    # De-para da própria tabela: a PK nova vem dali.
    if step.map_table:
        map_ref = dialect.qualified(plan.target_schema_name, step.map_table)
        old_key = step.old_key_expr or ""
        lines.append(
            f"JOIN {map_ref} {OWN_MAP_ALIAS} "
            f"ON {OWN_MAP_ALIAS}.{dialect.quote('old_key')} = {old_key}"
        )

    # De-para das FKs: traduz a chave antiga em chave nova.
    for fk in step.foreign_keys:
        if fk.parent_map is None:
            continue
        parent_map_ref = dialect.qualified(plan.target_schema_name, fk.parent_map)
        lines.append(
            f"LEFT JOIN {parent_map_ref} {fk.alias} "
            f"ON {fk.alias}.{dialect.quote('old_key')} = {fk.source_expr}"
        )

    conditions: list[str] = []
    if plan.idempotent and step.pk:
        pk_expr = step.pk_expr
        if pk_expr:
            filter_sql = dialect.idempotent_filter(target_ref, step.pk, pk_expr)
            if filter_sql:
                conditions.append(filter_sql)

    if conditions:
        lines.append("WHERE " + "\n  AND ".join(conditions))

    suffix = dialect.idempotent_suffix() if plan.idempotent else None
    if suffix:
        lines.append(suffix)

    lines[-1] = lines[-1] + ";"
    return lines


# ----------------------------------------------------------------------
# Constraints
# ----------------------------------------------------------------------
def render_constraints_sql(plan: MigrationPlan, target_schema: TargetSchema) -> str:
    dialect = get_dialect(plan.target_dialect)
    lines = [
        "-- Constraints de FK do destino.",
        "-- Rode DEPOIS da primeira carga: validar FK linha a linha antes da carga",
        "-- custa muito mais caro do que conferir o resultado no final.",
        "",
    ]
    migrated = {step.table for step in plan.steps}
    added = 0
    for step in plan.steps:
        if step.table not in migrated:
            continue
        for ref in target_schema.references():
            if ref.table != step.table or ref.ref_table not in migrated:
                continue
            if ref.field not in {column.target_field for column in step.columns}:
                continue
            name = dialect.constraint_name("fk", step.table, ref.field)
            constraint = dialect.foreign_key_constraint(
                step.table, ref.field, ref.ref_table, ref.ref_field, plan.target_schema_name
            )
            lines.extend(
                dialect.add_constraint(
                    plan.target_schema_name, step.table, name, constraint
                )
            )
            added += 1
    if added == 0:
        lines.append("-- (nenhuma FK entre as tabelas migradas)")
    return "\n".join(lines).rstrip() + "\n"


# ----------------------------------------------------------------------
# Validação
# ----------------------------------------------------------------------
def render_validation_sql(plan: MigrationPlan) -> str:
    dialect = get_dialect(plan.target_dialect)
    lines = [
        "-- Validação pós-carga. Cada consulta devolve 0 quando está tudo certo.",
        "",
        "-- 1. Contagem: origem x destino (devem ser iguais).",
    ]
    for step in plan.steps:
        source_ref = _source_ref(plan, step.source_table)
        target_ref = dialect.qualified(plan.target_schema_name, step.table)
        lines.append(
            f"SELECT '{step.table}' AS tabela, "
            f"(SELECT COUNT(*) FROM {source_ref}) AS origem, "
            f"(SELECT COUNT(*) FROM {target_ref}) AS destino;"
        )

    if any(step.foreign_keys for step in plan.steps):
        lines.extend(["", "-- 2. Órfãos de FK (devem ser 0)."])
        for step in plan.steps:
            for fk in step.foreign_keys:
                if fk.parent_map is None:
                    continue
                parent_ref = dialect.qualified(plan.target_schema_name, fk.parent_table)
                target_ref = dialect.qualified(plan.target_schema_name, step.table)
                parent_pk = _parent_pk(plan, fk)
                where = (
                    f"c.{dialect.quote(fk.column)} IS NOT NULL "
                    f"AND p.{dialect.quote(parent_pk)} IS NULL"
                )
                lines.append(
                    f"SELECT COUNT(*) AS orfaos_{step.table}_{fk.column} "
                    f"FROM {target_ref} c LEFT JOIN {parent_ref} p "
                    f"ON p.{dialect.quote(parent_pk)} = c.{dialect.quote(fk.column)} "
                    f"WHERE {where};"
                )

    lines.extend(
        ["", "-- 3. De-para não consumido (linhas mapeadas que não chegaram ao destino)."]
    )
    for step in plan.steps:
        if not step.map_table:
            continue
        map_ref = dialect.qualified(plan.target_schema_name, step.map_table)
        target_ref = dialect.qualified(plan.target_schema_name, step.table)
        pk = step.pk or "id"
        lines.append(
            f"SELECT COUNT(*) AS de_para_nao_consumido_{step.table} FROM {map_ref} m "
            f"WHERE NOT EXISTS (SELECT 1 FROM {target_ref} t "
            f"WHERE t.{dialect.quote(pk)} = m.{dialect.quote('new_id')});"
        )

    # FK da origem que não existe no de-para do pai: a carga grava NULL na
    # coluna (LEFT JOIN) e o resultado parece válido. Só a origem revela.
    orphan_fks = [
        (step, fk) for step in plan.steps for fk in step.foreign_keys if fk.parent_map
    ]
    if orphan_fks:
        lines.extend(
            [
                "",
                "-- 4. FK da origem sem correspondência no de-para do pai.",
                "--    Se algum contador acima de 0, a carga gravou NULL nessa coluna:",
                "--    o dado de origem referencia um registro que não foi migrado.",
            ]
        )
        for step, fk in orphan_fks:
            parent_map_ref = dialect.qualified(
                plan.target_schema_name, fk.parent_map or ""
            )
            source_ref = _source_ref(plan, step.source_table)
            lines.append(
                f"SELECT COUNT(*) AS fk_orfa_na_origem_{step.table}_{fk.column} "
                f"FROM {source_ref} s "
                f"WHERE {fk.source_expr} IS NOT NULL "
                f"AND NOT EXISTS (SELECT 1 FROM {parent_map_ref} m "
                f"WHERE m.{dialect.quote('old_key')} = {fk.source_expr});"
            )

    return "\n".join(lines).rstrip() + "\n"


# ----------------------------------------------------------------------
# Relatório
# ----------------------------------------------------------------------
def render_report(plan: MigrationPlan, mapping: MappingDocument) -> str:
    pending = [entry for entry in mapping.entries if entry.status == "pending_review"]
    rejected = [entry for entry in mapping.entries if entry.status == "rejected"]

    lines = [
        f"# Relatório de migração — {plan.project}",
        "",
        f"- Mapeamento: `{plan.mapping_id}`",
        f"- Target schema: versão `{plan.target_schema_version}`",
        f"- Campos aprovados e incluídos no script: {plan.entry_count}",
        f"- Campos ainda pendentes de revisão (NÃO incluídos): {len(pending)}",
        f"- Campos rejeitados: {len(rejected)}",
        f"- Tabelas na ordem de carga: {' -> '.join(step.table for step in plan.steps)}",
        "",
        "## Artefatos",
        "",
        "| arquivo | quando rodar |",
        "|---|---|",
        "| `etl_schema.sql` | antes da primeira carga (sequences + tabelas)"
        + (" |" if plan.create_schema else " — vazio: `create_schema: false` |"),
        "| `etl_script.sql` | a carga |",
        "| `etl_constraints.sql` | depois da carga (FKs) |",
        "| `etl_validation.sql` | depois da carga (contagens e órfãos) |",
        "",
    ]

    lines.append("## Tabelas")
    lines.append("")
    for step in plan.steps:
        if step.estimated_rows is None:
            rows = "volume desconhecido"
        else:
            rows = f"~{step.estimated_rows} linha(s)"
        extra = f", de-para em `{step.map_table}`" if step.map_table else ""
        lines.append(f"- `{step.table}` <- `{step.source_table}` ({rows}{extra})")

    missing = prerequisites(plan)
    if missing:
        lines.extend(["", "## Pré-requisitos na origem", ""])
        for item in missing:
            lines.append(f"- {item.splitlines()[0]}")
            lines.append("")
            lines.append("```sql")
            lines.extend(item.splitlines()[1:])
            lines.append("```")

    if not plan.create_schema:
        lines.extend(
            [
                "",
                "## Schema de destino gerenciado fora do pipeline",
                "",
                "`etl.create_schema: false` — `etl_schema.sql` não cria nada. Ele",
                "lista as tabelas e sequences que a carga assume existir. Confira",
                "que dão o mesmo resultado de `etl_schema.sql` com",
                "`create_schema: true` antes de rodar em produção.",
            ]
        )

    if plan.skipped:
        lines.extend(["", "## Tabelas não geradas", ""])
        for item in plan.skipped:
            lines.append(f"- `{item['table']}`: {item['reason']}")

    if plan.warnings:
        lines.extend(["", "## Avisos", ""])
        for warning in plan.warnings:
            lines.append(f"- {warning}")

    if pending:
        lines.extend(["", "## Pendências — revisar antes de rodar a migração final", ""])
        for entry in pending:
            lines.append(
                f"- `{entry.target_table}.{entry.target_field}` "
                f"(origem sugerida: {entry.source_table}.{entry.source_field}) — "
                f"{entry.review_reason}"
            )

    lines.extend(
        [
            "",
            "## Ordem de execução",
            "",
            "```sql",
            "-- 1",
            "\\i etl_schema.sql",
            "-- 2",
            "\\i etl_script.sql",
            "-- 3",
            "\\i etl_constraints.sql",
            "-- 4",
            "\\i etl_validation.sql",
            "```",
            "",
            "A contagem da etapa 4 deve bater com a origem e todo contador de órfão",
            "deve ser 0 — inclusive o de FK da origem sem de-para, que sinaliza NULL",
            "gravado onde o legado apontava para registro não migrado. Se a origem e o",
            "destino não estiverem no mesmo servidor,",
            "use `etl_script.sql` como especificação e migre via executor em lote",
            "(Fase 2b) — o script assume acesso aos dois lados na mesma conexão.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _source_ref(plan: MigrationPlan, table: str) -> str:
    if plan.source_schema:
        schema = quote_source(plan.source_dialect, plan.source_schema)
        return f"{schema}.{quote_source(plan.source_dialect, table)}"
    return quote_source(plan.source_dialect, table)


def _parent_pk(plan: MigrationPlan, fk: FkStep) -> str:
    for step in plan.steps:
        if step.table == fk.parent_table and step.pk:
            return step.pk
    return "id"
