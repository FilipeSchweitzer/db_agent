from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from app.agents.agent1_discovery import DiscoveryAgent
from app.agents.agent2_mapping import MappingAgent
from app.agents.agent3_etl import ETLAgent
from app.config import load_project_config
from app.db.connectors import get_engine
from app.llm import get_llm_client
from app.llm.client import LLMClient
from app.review.approval import review_fingerprint, verify_approval, write_approval
from app.review.export import export_to_xlsx, load_reviewed_xlsx
from app.schemas.mapping import MappingDocument, ReviewStatus
from app.schemas.profiling import DiscoveryReport
from app.schemas.target_schema import TargetSchema

# Sobrescrevível para rodar fora do container (teste, CI, uso local).
OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", "/app/output"))

ProgressCallback = Callable[[int, int, str], None]


def _output_dir(project: str) -> Path:
    d = OUTPUT_ROOT / project
    d.mkdir(parents=True, exist_ok=True)
    return d


def _record_run(
    project: str,
    phase: str,
    llm: LLMClient | None,
    started: float,
    **extra: object,
) -> Path:
    """Acrescenta uma entrada em output/<projeto>/run_log.json.

    O log é cumulativo (lista `runs`), então discovery, mapping e generate do
    mesmo projeto ficam auditáveis no mesmo arquivo, com tokens, latência e
    tentativas de cada chamada de LLM.
    """
    out_dir = _output_dir(project)
    path = out_dir / "run_log.json"
    log = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"runs": []}

    log["runs"].append(
        {
            "phase": phase,
            "started_at": datetime.now(UTC).isoformat(),
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "llm": llm.telemetry() if llm is not None else None,
            **extra,
        }
    )
    path.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def run_discovery(client_name: str) -> Path:
    cfg = load_project_config(client_name)
    started = time.perf_counter()
    engine = get_engine(cfg.source, read_only=True)
    llm = get_llm_client()

    report = DiscoveryAgent(llm).run(engine, cfg.source.dialect, cfg.scope_tables or None)

    out = _output_dir(cfg.project) / "discovery_report.json"
    out.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    _record_run(
        cfg.project,
        "discovery",
        llm,
        started,
        artifact=out.name,
        tables=len(report.tables),
    )
    return out


def run_mapping(client_name: str, on_table: ProgressCallback | None = None) -> Path:
    cfg = load_project_config(client_name)
    started = time.perf_counter()
    llm = get_llm_client()

    discovery_path = _output_dir(cfg.project) / "discovery_report.json"
    discovery = DiscoveryReport.model_validate_json(discovery_path.read_text())

    target_schema = TargetSchema.load(cfg.target_schema_path)

    mapping = MappingAgent(llm).run(
        cfg.project, discovery, target_schema, on_table=on_table
    )

    out_dir = _output_dir(cfg.project)
    mapping_json = out_dir / "mapping.json"
    mapping_json.write_text(mapping.model_dump_json(indent=2), encoding="utf-8")

    review_xlsx = out_dir / "mapeamento_para_revisao.xlsx"
    export_to_xlsx(mapping, review_xlsx)

    # Prova de que o mapping.json revisado é este, e não um regenerado depois.
    write_approval(
        out_dir,
        project=cfg.project,
        mapping_id=mapping.mapping_id,
        target_schema_version=target_schema.version,
        entries=len(mapping.entries),
    )

    _record_run(
        cfg.project,
        "mapping",
        llm,
        started,
        artifact=review_xlsx.name,
        target_tables=len(target_schema.tables),
        entries=len(mapping.entries),
        unmapped=len(mapping.unmapped_target_fields),
        warnings=len(mapping.warnings),
    )
    return review_xlsx


def run_etl_generation(
    client_name: str,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> Path:
    """Só deve ser chamado depois que o analista revisou e salvou a planilha
    (mapeamento_para_revisao.xlsx) com a coluna `status` atualizada."""
    cfg = load_project_config(client_name)
    started = time.perf_counter()
    out_dir = _output_dir(cfg.project)

    approval = {} if force else verify_approval(out_dir)

    mapping = MappingDocument.model_validate_json((out_dir / "mapping.json").read_text())
    mapping = load_reviewed_xlsx(mapping, out_dir / "mapeamento_para_revisao.xlsx")
    target_schema = TargetSchema.load(cfg.target_schema_path)

    result = ETLAgent().run(
        mapping,
        target_schema=target_schema,
        source_dialect=cfg.source.dialect,
        target_dialect=cfg.target.dialect,
        output_dir=out_dir,
        source_schema=cfg.source.schema_name,
        target_schema_name=cfg.target.schema_name,
        discovery=_load_discovery(out_dir),
        etl=cfg.etl,
        dry_run=dry_run,
    )

    fingerprint = review_fingerprint(mapping)
    if not dry_run:
        _append_audit(out_dir / "relatorio.md", approval, fingerprint)

    _record_run(
        cfg.project,
        "generate",
        None,
        started,
        dry_run=dry_run,
        entries=result.plan.entry_count,
        approved=sum(
            1
            for entry in mapping.entries
            if entry.status in (ReviewStatus.approved, ReviewStatus.edited)
        ),
        tables=[step.table for step in result.plan.steps],
        skipped=len(result.plan.skipped),
        warnings=len(result.plan.warnings),
        mapping_sha256=approval.get("mapping_sha256"),
        review_sha256=fingerprint,
        artifacts=sorted(path.name for path in result.written.values()),
    )

    return result.written["etl_plan.json"] if dry_run else out_dir / "etl_script.sql"


def _load_discovery(out_dir: Path) -> DiscoveryReport | None:
    path = out_dir / "discovery_report.json"
    if not path.exists():
        return None
    return DiscoveryReport.model_validate_json(path.read_text())


def _append_audit(report: Path, approval: dict, fingerprint: str) -> None:
    lines = [
        "",
        "## Auditoria",
        "",
        f"- `mapping.json` (sha256): `{approval.get('mapping_sha256', 'não verificado')}`",
        f"- estado revisado (sha256): `{fingerprint}`",
        f"- target schema: `{approval.get('target_schema_version', 'desconhecida')}`",
    ]
    with report.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
