"""Agente 3 — transforma o mapeamento APROVADO em artefatos de migração.

Só considera entradas com status `approved` ou `edited`. `pending_review` e
`rejected` aparecem no relatório como pendência e ficam fora do SQL — este
agente nunca decide por conta própria o que incluir.

A geração em si vive em `app/etl/`: aqui só há o recorte de aprovação, a
escrita dos arquivos e o modo dry-run.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.config import ETLConfig
from app.etl import generator
from app.etl import plan as plan_module
from app.schemas.mapping import MappingDocument
from app.schemas.profiling import DiscoveryReport
from app.schemas.target_schema import TargetSchema

PLAN_FILE = "etl_plan.json"


@dataclass
class ETLResult:
    plan: plan_module.MigrationPlan
    dry_run: bool
    written: dict[str, Path] = field(default_factory=dict)


class ETLAgent:
    def run(
        self,
        mapping: MappingDocument,
        *,
        target_schema: TargetSchema,
        source_dialect: str,
        target_dialect: str,
        output_dir: Path,
        source_schema: str | None = None,
        target_schema_name: str | None = None,
        discovery: DiscoveryReport | None = None,
        etl: ETLConfig | None = None,
        dry_run: bool = False,
    ) -> ETLResult:
        plan = plan_module.build_plan(
            mapping=mapping,
            target_schema=target_schema,
            source_dialect=source_dialect,
            target_dialect=target_dialect,
            source_schema=source_schema,
            target_schema_name=target_schema_name,
            discovery=discovery,
            etl=etl,
        )

        output_dir.mkdir(parents=True, exist_ok=True)
        plan_json = json.dumps(plan.to_artifact(), indent=2, ensure_ascii=False)
        written = {PLAN_FILE: _write(output_dir / PLAN_FILE, plan_json)}

        if dry_run:
            return ETLResult(plan=plan, dry_run=True, written=written)

        artifacts = generator.generate(plan, target_schema, mapping)
        for name, content in artifacts.files().items():
            written[name] = _write(output_dir / name, content)

        return ETLResult(plan=plan, dry_run=False, written=written)


def _write(path: Path, content: str) -> Path:
    path.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
    return path
