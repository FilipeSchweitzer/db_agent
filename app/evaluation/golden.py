"""Caso golden: o mapeamento manual correto, congelado como dado.

Um caso é uma pasta com quatro coisas:

```
eval/<caso>/
├── golden.yaml            # o que é certo: mapeamento campo a campo + discovery
├── target_schema.yaml     # schema de destino contra o qual o caso foi mapeado
└── discovery_report.json  # perfilamento congelado (sem banco na avaliação)
```

Congelar o discovery é o que torna a avaliação reprodutível: rodar o Agente 2
duas vezes com o mesmo caso mede prompt/agente, não a variação do banco.

O `golden.yaml` é a fonte da verdade — inclusive para os campos que devem
**ficar sem origem**. `load_case()` exige que todos os campos do schema
apareçam, porque um golden incompleto transforma "não avaliei" em "acertou".
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError, model_validator

from app.evaluation.metrics import normalize_name
from app.privacy.report import sanitize_discovery
from app.schemas.mapping import MappingDocument, MappingEntry
from app.schemas.profiling import DiscoveryReport
from app.schemas.target_schema import TargetSchema
from app.transforms.registry import available_transform_names

GOLDEN_FILE = "golden.yaml"
SCHEMA_FILE = "target_schema.yaml"
DISCOVERY_FILE = "discovery_report.json"

# Casos reais podem ficar fora do repositório (nome de tabela de cliente não
# é dado público). `EVAL_ROOT=/caminho/privado` aponta para lá.
EVAL_ROOT = Path(os.environ.get("EVAL_ROOT", "eval"))


class GoldenError(ValueError):
    """O caso golden está incompleto ou inconsistente."""


def load_discovery_report(path: Path) -> DiscoveryReport:
    """Lê e valida um relatório de discovery, com erro de entrada legível."""
    try:
        return DiscoveryReport.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise GoldenError(f"{path}: relatório de discovery inválido: {exc}") from exc


class GoldenMapping(BaseModel):
    """Um campo de destino e a origem que o mapeamento manual escolheu.

    `source_table`/`source_field` nulos significam "este campo deve ficar sem
    origem". `transformation` só é avaliada quando a chave aparece no YAML:
    ausente = não avaliar; `null` = deve sair sem transformação.
    """

    target_table: str
    target_field: str
    source_table: str | None = None
    source_field: str | None = None
    transformation: str | None = None

    @model_validator(mode="after")
    def _source_is_all_or_nothing(self):
        if (self.source_table is None) != (self.source_field is None):
            raise ValueError(
                f"{self.target_table}.{self.target_field}: "
                "source_table e source_field vêm juntos (ou ambos nulos)"
            )
        return self


class GoldenColumn(BaseModel):
    """Expectativa de discovery para uma coluna da origem.

    Campo ausente = não avaliar. É assim que um caso pode medir só o
    mapeamento, ou só a flag de PII, sem inventar expectativa para o resto.
    """

    table: str
    column: str
    inferred_type: str | None = None
    pii: bool | None = None


class GoldenCase(BaseModel):
    case: str
    description: str = ""
    synthetic: bool = False
    source_dialect: str | None = None
    target_schema_version: str | None = None
    mapping: list[GoldenMapping]
    discovery: list[GoldenColumn] = []

    @model_validator(mode="after")
    def _no_duplicates(self):
        seen: set[tuple[str, str]] = set()
        for row in self.mapping:
            key = (normalize_name(row.target_table), normalize_name(row.target_field))
            if key in seen:
                raise ValueError(
                    f"campo repetido em mapping: {row.target_table}.{row.target_field}"
                )
            seen.add(key)

        seen_columns: set[tuple[str, str]] = set()
        for row in self.discovery:
            key = (normalize_name(row.table), normalize_name(row.column))
            if key in seen_columns:
                raise ValueError(f"coluna repetida em discovery: {row.table}.{row.column}")
            seen_columns.add(key)
        return self


@dataclass
class LoadedCase:
    path: Path
    golden: GoldenCase
    target_schema: TargetSchema
    discovery: DiscoveryReport
    warnings: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.golden.case

    def schema_fields(self) -> list[str]:
        return [
            f"{table}.{field_name}"
            for table, spec in self.target_schema.tables.items()
            for field_name in spec.fields
        ]


def resolve_case_ref(reference: str) -> Path:
    """Aceita `eval/<caso>` (nome) ou um caminho de pasta do caso."""
    path = Path(reference).expanduser()
    if path.is_absolute() or path.exists():
        return path
    return EVAL_ROOT / reference


def load_case(case_path: str | Path) -> LoadedCase:
    """Carrega e valida um caso; falha cedo se o golden não cobre o schema."""
    base = Path(case_path)
    golden_path = base / GOLDEN_FILE
    schema_path = base / SCHEMA_FILE
    discovery_path = base / DISCOVERY_FILE

    for path in (golden_path, schema_path, discovery_path):
        if not path.is_file():
            raise GoldenError(f"caso incompleto: falta {path}")

    try:
        raw = yaml.safe_load(golden_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise GoldenError(f"{golden_path}: YAML inválido: {exc}") from exc
    if not isinstance(raw, dict):
        raise GoldenError(f"{golden_path}: esperado um mapa YAML no topo")

    try:
        golden = GoldenCase.model_validate(raw)
    except ValidationError as exc:
        raise GoldenError(f"{golden_path}: {exc}") from exc

    # Fronteira de entrada: qualquer falha ao carregar o schema do caso vira
    # GoldenError, para o CLI não vazar traceback de YAML/Pydantic.
    try:
        target_schema = TargetSchema.load(schema_path)
    except Exception as exc:
        raise GoldenError(f"{schema_path}: target_schema inválido: {exc}") from exc
    report = load_discovery_report(discovery_path)
    warnings: list[str] = []

    _validate_against_schema(golden, target_schema)
    _validate_transformations(golden)

    if (
        golden.target_schema_version
        and golden.target_schema_version != target_schema.version
    ):
        warnings.append(
            f"golden.yaml declara target_schema_version="
            f"{golden.target_schema_version!r}, mas {SCHEMA_FILE} está na versão "
            f"{target_schema.version!r}"
        )

    if (
        golden.source_dialect
        and normalize_name(golden.source_dialect) != normalize_name(report.source_dialect)
    ):
        raise GoldenError(
            f"golden.yaml declara source_dialect={golden.source_dialect!r}, mas o "
            f"relatório de discovery é {report.source_dialect!r}"
        )

    return LoadedCase(
        path=base,
        golden=golden,
        target_schema=target_schema,
        discovery=report,
        warnings=warnings,
    )


def _validate_against_schema(golden: GoldenCase, target_schema: TargetSchema) -> None:
    schema_keys = {
        (normalize_name(table), normalize_name(field_name))
        for table, spec in target_schema.tables.items()
        for field_name in spec.fields
    }
    golden_keys = {
        (normalize_name(row.target_table), normalize_name(row.target_field))
        for row in golden.mapping
    }

    missing = [
        f"{table}.{field}"
        for table, spec in target_schema.tables.items()
        for field_name in spec.fields
        if (normalize_name(table), normalize_name(field_name)) not in golden_keys
    ]
    if missing:
        preview = ", ".join(missing[:10])
        suffix = f" (+{len(missing) - 10})" if len(missing) > 10 else ""
        raise GoldenError(
            "golden.yaml não cobre todos os campos do target_schema: "
            f"{preview}{suffix}. Liste também os que devem ficar sem origem "
            "(source_table/source_field nulos)."
        )

    unknown = [
        f"{row.target_table}.{row.target_field}"
        for row in golden.mapping
        if (normalize_name(row.target_table), normalize_name(row.target_field))
        not in schema_keys
    ]
    if unknown:
        raise GoldenError(
            "golden.yaml cita campo que não existe no target_schema: "
            + ", ".join(unknown)
        )


def _validate_transformations(golden: GoldenCase) -> None:
    known = set(available_transform_names())
    unknown = sorted(
        {
            row.transformation
            for row in golden.mapping
            if row.transformation is not None and row.transformation not in known
        }
    )
    if unknown:
        raise GoldenError(
            "transformação desconhecida em golden.yaml: "
            + ", ".join(unknown)
            + f" (disponíveis: {', '.join(sorted(known))})"
        )


def scaffold_case(
    case_dir: str | Path,
    *,
    schema_path: str | Path,
    discovery_path: str | Path,
    mapping_path: str | Path,
    project: str,
    force: bool = False,
) -> Path:
    """Cria um caso a partir de um `map` já executado, para revisão manual.

    O golden nasce do mapeamento **proposto** — é um rascunho, não a verdade.
    A revisão humana é o que o transforma em golden set de verdade; por isso o
    cabeçalho do arquivo pede a conferência contra a migração manual.
    """
    destination = Path(case_dir)
    golden_path = destination / GOLDEN_FILE
    if golden_path.exists() and not force:
        raise GoldenError(
            f"{golden_path} já existe; use --force para sobrescrever o rascunho"
        )

    schema_source = Path(schema_path)
    target_schema = TargetSchema.load(schema_source)
    discovery = sanitize_discovery(
        DiscoveryReport.model_validate_json(Path(discovery_path).read_text(encoding="utf-8"))
    )
    mapping = MappingDocument.model_validate_json(
        Path(mapping_path).read_text(encoding="utf-8")
    )

    predictions: dict[tuple[str, str], MappingEntry] = {}
    for entry in mapping.entries:
        key = (normalize_name(entry.target_table), normalize_name(entry.target_field))
        predictions[key] = entry

    rows: list[dict] = []
    for table, spec in target_schema.tables.items():
        for field_name in spec.fields:
            entry = predictions.get((normalize_name(table), normalize_name(field_name)))
            row: dict = {
                "target_table": table,
                "target_field": field_name,
                "source_table": entry.source_table if entry is not None else None,
                "source_field": entry.source_field if entry is not None else None,
            }
            if entry is not None and entry.transformation is not None:
                row["transformation"] = entry.transformation.type
            rows.append(row)

    destination.mkdir(parents=True, exist_ok=True)
    if Path(schema_source).resolve() != (destination / SCHEMA_FILE).resolve():
        shutil.copyfile(schema_source, destination / SCHEMA_FILE)
    (destination / DISCOVERY_FILE).write_text(
        discovery.model_dump_json(indent=2), encoding="utf-8"
    )

    data = {
        "case": destination.name,
        "description": (
            f"Rascunho criado a partir de output/{project}/mapping.json — "
            "revisar contra a migração manual antes de usar como golden set."
        ),
        "synthetic": False,
        "source_dialect": discovery.source_dialect,
        "target_schema_version": target_schema.version,
        "mapping": rows,
        "discovery": [],
    }
    header = (
        "# Golden set: revise CADA linha contra o mapeamento manual.\n"
        "# - source_table/source_field nulos = o campo deve ficar sem origem.\n"
        "# - transformation: omitida = não avaliar; null = deve sair sem transformação.\n"
        "# - discovery: opcional; preencha inferred_type/pii para medir o Agente 1.\n"
    )
    golden_path.write_text(
        header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return golden_path
