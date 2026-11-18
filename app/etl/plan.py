"""Plano de migração: o que será inserido, em que ordem e com qual chave.

A geração de SQL (`app/etl/generator.py`) não decide nada — ela só escreve o
que este módulo resolveu. Assim o plano pode ser inspecionado (dry-run,
`etl_plan.json`) sem executar nada, e o SQL fica testável por asserção.

Regras que valem para qualquer dialeto:

- só entram entradas aprovadas/editadas que tenham origem declarada;
- uma tabela de destino só vira passo se todas as suas entradas vierem da
  mesma tabela de origem (JOIN multi-origem continua manual);
- tabela referenciada é carregada antes de quem a referencia (ordem
  topológica do target schema);
- PK gerada por sequence ganha tabela de-para `_map_<tabela>`, que traduz a
  chave antiga na chave nova — é isso que faz `pessoa.cod_bairro` apontar
  para o bairro certo depois da migração, e é isso que torna a segunda
  execução idempotente (a chave nova é estável);
- FK cuja tabela referenciada não está sendo migrada falha alto, em vez de
  copiar a chave antiga para o destino.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.config import ETLConfig
from app.schemas.mapping import MappingDocument, MappingEntry, ReviewStatus
from app.schemas.profiling import DiscoveryReport
from app.schemas.target_schema import TargetSchema
from app.transforms.registry import render_sql

STAGING_PREFIX = "_map_"
# Alias do de-para da própria tabela no SELECT de carga.
OWN_MAP_ALIAS = "m_self"


class PlanError(RuntimeError):
    """O mapeamento aprovado não permite gerar um script correto."""


@dataclass(frozen=True)
class ColumnStep:
    target_field: str
    type_spec: str
    source_expr: str
    is_pk: bool = False
    # True quando o valor vem da tabela de-para, não direto da origem.
    via_map: bool = False


@dataclass(frozen=True)
class FkStep:
    """FK do destino traduzida pelo de-para da tabela referenciada."""

    column: str
    parent_table: str
    parent_field: str
    parent_map: str | None
    source_expr: str

    @property
    def alias(self) -> str:
        # Um alias por coluna: a mesma tabela pode ter duas FKs para o mesmo pai.
        return f"m_{self.column}"


@dataclass
class TableStep:
    table: str
    source_table: str
    columns: list[ColumnStep]
    pk: str | None
    sequence: str | None
    map_table: str | None
    old_key_type: str
    # Expressão da chave antiga na origem — alimenta o de-para.
    old_key_expr: str | None
    estimated_rows: int | None
    foreign_keys: list[FkStep] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def pk_column(self) -> ColumnStep | None:
        for column in self.columns:
            if column.target_field == self.pk:
                return column
        return None

    @property
    def pk_expr(self) -> str | None:
        column = self.pk_column
        return column.source_expr if column else None


@dataclass
class MigrationPlan:
    project: str
    mapping_id: str
    target_schema_version: str
    source_dialect: str
    target_dialect: str
    source_schema: str | None
    target_schema_name: str | None
    steps: list[TableStep]
    skipped: list[dict[str, str]]
    warnings: list[str]
    entry_count: int
    idempotent: bool
    create_schema: bool

    def to_artifact(self) -> dict:
        """Forma serializável (`etl_plan.json`)."""
        return {
            "project": self.project,
            "mapping_id": self.mapping_id,
            "target_schema_version": self.target_schema_version,
            "source_dialect": self.source_dialect,
            "target_dialect": self.target_dialect,
            "source_schema": self.source_schema,
            "target_schema": self.target_schema_name,
            "idempotent": self.idempotent,
            "create_schema": self.create_schema,
            "entry_count": self.entry_count,
            "warnings": self.warnings,
            "skipped": self.skipped,
            "tables": [
                {
                    "table": step.table,
                    "source_table": step.source_table,
                    "primary_key": step.pk,
                    "sequence": step.sequence,
                    "staging_table": step.map_table,
                    "estimated_rows": step.estimated_rows,
                    "fields": [column.target_field for column in step.columns],
                    "foreign_keys": [
                        {
                            "column": fk.column,
                            "references": f"{fk.parent_table}.{fk.parent_field}",
                            "translated": fk.parent_map is not None,
                        }
                        for fk in step.foreign_keys
                    ],
                    "warnings": step.warnings,
                }
                for step in self.steps
            ],
        }


def build_plan(
    *,
    mapping: MappingDocument,
    target_schema: TargetSchema,
    source_dialect: str,
    target_dialect: str,
    source_schema: str | None = None,
    target_schema_name: str | None = None,
    discovery: DiscoveryReport | None = None,
    etl: ETLConfig | None = None,
) -> MigrationPlan:
    etl = etl or ETLConfig()
    approved = [
        entry
        for entry in mapping.entries
        if entry.status in (ReviewStatus.approved, ReviewStatus.edited)
    ]
    if not approved:
        raise PlanError(
            "Nenhuma entrada aprovada/editada na planilha — o script ficaria vazio."
        )

    grouped = _group_by_target_table(approved)
    warnings: list[str] = []
    skipped: list[dict[str, str]] = []
    prepared: dict[str, tuple[str, list[MappingEntry]]] = {}

    for table in target_schema.tables:
        entries = grouped.get(table)
        if not entries:
            continue
        usable, drop_warnings = _usable_entries(table, entries)
        warnings.extend(drop_warnings)
        if not usable:
            skipped.append({"table": table, "reason": "sem entradas utilizáveis"})
            continue
        source_tables = {entry.source_table for entry in usable}
        if len(source_tables) != 1:
            skipped.append(
                {
                    "table": table,
                    "reason": f"depende de múltiplas tabelas de origem: {sorted(source_tables)}",
                }
            )
            continue
        prepared[table] = (source_tables.pop() or "", usable)

    if not prepared:
        raise PlanError(
            "Nenhuma tabela de destino pôde ser gerada. Motivos: "
            + "; ".join(f"{item['table']}: {item['reason']}" for item in skipped)
        )

    order = target_schema.dependency_order(prepared.keys())
    rows_by_table = _row_counts(discovery)
    null_rates = _null_rates(discovery)
    strategies = _pk_strategies(target_schema, prepared, etl, warnings)
    unresolved: list[str] = []

    steps: list[TableStep] = []
    for table in order:
        source_table, entries = prepared[table]
        step = _build_step(
            table=table,
            source_table=source_table,
            entries=entries,
            target_schema=target_schema,
            source_dialect=source_dialect,
            target_dialect=target_dialect,
            strategy=strategies[table],
            migrated=set(prepared),
            strategies=strategies,
            sequence=_sequence_name(etl, table)
            if strategies[table] in ("sequence", "unmapped")
            else None,
            estimated_rows=rows_by_table.get(source_table)
            or rows_by_table.get(source_table.upper()),
            null_rates=null_rates,
            unresolved=unresolved,
            etl=etl,
        )
        steps.append(step)
        warnings.extend(f"{table}: {item}" for item in step.warnings)

    if unresolved and not etl.allow_unresolved_fks:
        raise PlanError(
            "FK apontando para tabela que não está sendo migrada — a chave antiga "
            "seria copiada sem tradução:\n  "
            + "\n  ".join(unresolved)
            + "\nMapeie e aprove a tabela referenciada, ou defina "
            "etl.allow_unresolved_fks: true para assumir o risco."
        )

    for step in steps:
        if step.estimated_rows and step.estimated_rows > etl.high_volume_rows:
            warnings.append(
                f"{step.table}: ~{step.estimated_rows} linhas — a carga roda em uma "
                "transação por tabela; volume alto pede execução em lotes (Fase 2b)."
            )

    return MigrationPlan(
        project=mapping.project,
        mapping_id=mapping.mapping_id,
        target_schema_version=target_schema.version,
        source_dialect=source_dialect,
        target_dialect=target_dialect,
        source_schema=source_schema,
        target_schema_name=target_schema_name,
        steps=steps,
        skipped=skipped,
        warnings=warnings,
        entry_count=sum(len(step.columns) for step in steps),
        idempotent=etl.idempotent,
        create_schema=etl.create_schema,
    )


# ----------------------------------------------------------------------
# Internos
# ----------------------------------------------------------------------
def _group_by_target_table(entries: list[MappingEntry]) -> dict[str, list[MappingEntry]]:
    grouped: dict[str, list[MappingEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.target_table, []).append(entry)
    return grouped


def _usable_entries(
    table: str, entries: list[MappingEntry]
) -> tuple[list[MappingEntry], list[str]]:
    usable: list[MappingEntry] = []
    warnings: list[str] = []
    for entry in entries:
        if not entry.source_table or not entry.source_field:
            warnings.append(
                f"{table}.{entry.target_field}: aprovada sem origem declarada — ignorada."
            )
            continue
        usable.append(entry)
    return usable, warnings


def _pk_strategies(
    target_schema: TargetSchema,
    prepared: dict[str, tuple[str, list[MappingEntry]]],
    etl: ETLConfig,
    warnings: list[str],
) -> dict[str, str]:
    """Como a PK de cada tabela é obtida: sequence, preserve ou unmapped.

    `unmapped` é o caso sem origem aprovada para a PK: não existe chave antiga
    para alimentar o de-para nem para copiar, então o banco decide — e nenhuma
    FK pode apontar para essa tabela.
    """
    strategies: dict[str, str] = {}
    for table, (_, entries) in prepared.items():
        strategy = etl.pk_strategy.get(table) or target_schema.pk_strategy_default(table)
        pk = target_schema.tables[table].primary_key
        has_source = pk is not None and any(e.target_field == pk for e in entries)
        if strategy == "sequence":
            if pk is None:
                warnings.append(f"{table}: sem PK declarada no target schema — usando 'preserve'.")
                strategy = "preserve"
            elif not target_schema.tables[table].fields[pk].is_integer:
                warnings.append(
                    f"{table}.{pk}: PK não é inteira — não dá para gerar por sequence; "
                    "usando 'preserve'."
                )
                strategy = "preserve"
            elif not has_source:
                warnings.append(
                    f"{table}: PK '{pk}' sem origem aprovada — não há como construir o "
                    "de-para; a chave será gerada pelo banco e nenhuma FK pode "
                    "referenciar esta tabela nesta execução."
                )
                strategy = "unmapped"
        strategies[table] = strategy
    return strategies


def _sequence_name(etl: ETLConfig, table: str) -> str:
    return etl.sequence_pattern.format(table=table)


def _build_step(
    *,
    table: str,
    source_table: str,
    entries: list[MappingEntry],
    target_schema: TargetSchema,
    source_dialect: str,
    target_dialect: str,
    strategy: str,
    migrated: set[str],
    strategies: dict[str, str],
    sequence: str | None,
    estimated_rows: int | None,
    null_rates: dict[tuple[str, str], float],
    unresolved: list[str],
    etl: ETLConfig,
) -> TableStep:
    schema_table = target_schema.tables[table]
    pk = schema_table.primary_key
    warnings: list[str] = []
    entries_by_field = {entry.target_field: entry for entry in entries}

    columns: list[ColumnStep] = []
    for field_name, target_field in schema_table.fields.items():
        entry = entries_by_field.get(field_name)
        if entry is None:
            if target_field.required and field_name != pk:
                warnings.append(f"{field_name}: obrigatório no destino e sem entrada aprovada.")
            continue
        if target_field.required and entry.source_field:
            rate = null_rates.get(
                (source_table.upper(), entry.source_field.upper()), 0.0
            )
            if rate > 0:
                warnings.append(
                    f"{field_name}: obrigatório no destino, mas {rate:.0%} dos valores "
                    f"de {entry.source_field} são nulos — a carga falha até tratar isso."
                )
        columns.append(
            ColumnStep(
                target_field=field_name,
                type_spec=target_field.type,
                source_expr=_source_expr(entry, source_dialect),
                is_pk=field_name == pk,
            )
        )

    # PK por sequence não é copiada da origem: o INSERT inclui a coluna, mas o
    # valor vem da tabela de-para.
    map_table = f"{STAGING_PREFIX}{table}" if strategy == "sequence" and sequence else None
    old_key_expr: str | None = None
    if pk is not None and pk not in entries_by_field:
        if strategy == "sequence":
            columns.insert(0, ColumnStep(target_field=pk, type_spec=schema_table.fields[pk].type,
                                         source_expr="", is_pk=True, via_map=True))
            warnings.append(f"{pk}: PK gerada pelo destino; valor da origem é ignorado.")
        else:
            warnings.append(
                f"{pk}: PK sem entrada aprovada — o INSERT omitirá a chave e o banco "
                "decidirá; idempotência não garantida."
            )

    # O de-para precisa da expressão da chave antiga, capturada antes de a
    # coluna da PK ser trocada pelo valor novo.
    pk_column = _column(columns, pk or "")
    if pk_column is not None:
        old_key_expr = pk_column.source_expr or None

    # Tipo da chave antiga no staging: mesma família da PK de destino.
    old_key_type = schema_table.fields[pk].type if pk else "integer"
    quote = _quote_for(target_dialect)

    # FK entre tabelas migradas -> junta com o de-para da tabela referenciada.
    foreign_keys: list[FkStep] = []
    for ref in target_schema.references():
        if ref.table != table:
            continue
        column = _column(columns, ref.field)
        if column is None:
            continue  # campo não aprovado: não entra no INSERT, não precisa de FK
        if ref.ref_table not in migrated:
            if not etl.allow_unresolved_fks:
                unresolved.append(f"{table}.{ref.field} -> {ref.ref_table}.{ref.ref_field}")
                continue
            warnings.append(
                f"{ref.field}: FK para '{ref.ref_table}', que não está sendo migrada — "
                "o valor da origem é copiado como está."
            )
            continue
        parent_strategy = strategies[ref.ref_table]
        if parent_strategy == "unmapped":
            # A PK do pai é gerada sem de-para: a chave antiga não tem
            # correspondente conhecido, então copiá-la apontaria para o nada.
            if not etl.allow_unresolved_fks:
                unresolved.append(
                    f"{table}.{ref.field} -> {ref.ref_table}.{ref.ref_field} "
                    f"(PK de '{ref.ref_table}' sem origem aprovada)"
                )
                continue
            warnings.append(
                f"{ref.field}: FK para '{ref.ref_table}', cuja PK não tem de-para — "
                "o valor da origem é copiado como está e provavelmente não existe lá."
            )
            continue
        if parent_strategy == "sequence":
            fk = FkStep(
                column=ref.field,
                parent_table=ref.ref_table,
                parent_field=ref.ref_field,
                parent_map=f"{STAGING_PREFIX}{ref.ref_table}",
                source_expr=column.source_expr,
            )
            foreign_keys.append(fk)
            _replace_column(
                columns,
                ref.field,
                ColumnStep(
                    target_field=column.target_field,
                    type_spec=column.type_spec,
                    source_expr=f"{fk.alias}.{quote('new_id')}",
                    is_pk=column.is_pk,
                    via_map=True,
                ),
            )
        else:
            warnings.append(
                f"{ref.field}: FK para '{ref.ref_table}' copiada sem tradução "
                "(a PK da referenciada é preservada)."
            )

    if strategy == "sequence" and sequence is not None and map_table is not None:
        pk_column = _column(columns, pk or "")
        if pk_column is not None and not pk_column.via_map:
            _replace_column(
                columns,
                pk_column.target_field,
                ColumnStep(
                    target_field=pk_column.target_field,
                    type_spec=pk_column.type_spec,
                    source_expr=f"{OWN_MAP_ALIAS}.{quote('new_id')}",
                    is_pk=True,
                    via_map=True,
                ),
            )

    return TableStep(
        table=table,
        source_table=source_table,
        columns=columns,
        pk=pk,
        sequence=sequence,
        map_table=map_table,
        old_key_type=old_key_type,
        old_key_expr=old_key_expr,
        estimated_rows=estimated_rows,
        foreign_keys=foreign_keys,
        warnings=warnings,
    )


def _column(columns: list[ColumnStep], field: str) -> ColumnStep | None:
    for column in columns:
        if column.target_field == field:
            return column
    return None


def _replace_column(columns: list[ColumnStep], field: str, new: ColumnStep) -> None:
    for index, column in enumerate(columns):
        if column.target_field == field:
            columns[index] = new
            return


def _source_expr(entry: MappingEntry, source_dialect: str) -> str:
    from app.etl.dialects import source_column

    column = source_column(source_dialect, "s", entry.source_field or "")
    if entry.transformation is None:
        return column
    return render_sql(
        entry.transformation.type,
        source_dialect,
        column,
        entry.transformation.params,
    )


def _quote_for(target_dialect: str):
    from app.etl.dialects import get_dialect

    return get_dialect(target_dialect).quote


def _row_counts(discovery: DiscoveryReport | None) -> dict[str, int]:
    if discovery is None:
        return {}
    return {table.table: table.row_count for table in discovery.tables}


def _null_rates(discovery: DiscoveryReport | None) -> dict[tuple[str, str], float]:
    """Taxa de nulos por (tabela, coluna) da origem, em maiúsculas."""
    if discovery is None:
        return {}
    return {
        (table.table.upper(), column.name.upper()): column.null_rate
        for table in discovery.tables
        for column in table.columns
    }
