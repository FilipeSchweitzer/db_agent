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
from app.privacy.audit import (
    build_record,
    hash_artifacts,
    pii_summary,
    resolve_approver,
    write_audit_record,
)
from app.privacy.policy import enforce_pii_backend_policy
from app.privacy.report import discovery_pii_columns, sanitize_discovery
from app.review.approval import (
    check_schema_compatibility,
    file_sha256,
    review_fingerprint,
    verify_approval,
    write_approval,
)
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
    started_at: datetime | None = None,
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
            "started_at": (started_at or datetime.now(UTC)).isoformat(),
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "llm": llm.telemetry() if llm is not None else None,
            **extra,
        }
    )
    path.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _record_audit(
    project: str,
    phase: str,
    started: float,
    started_at: datetime,
    *,
    llm: LLMClient | None = None,
    pii_columns: list[str] | None = None,
    **extra: object,
) -> Path:
    """Grava a entrada de auditoria da execução (ver `app/privacy/audit.py`)."""
    record = build_record(
        project=project,
        phase=phase,
        started_at=started_at,
        duration_ms=(time.perf_counter() - started) * 1000,
        pii=pii_summary(pii_columns),
        llm=llm,
        **extra,
    )
    return write_audit_record(_output_dir(project), record)


def run_discovery(client_name: str) -> Path:
    cfg = load_project_config(client_name)
    started = time.perf_counter()
    started_at = datetime.now(UTC)
    engine = get_engine(cfg.source, read_only=True)

    # O cliente LLM é construído só depois do perfilamento e da política de
    # PII (dentro de `run`), para que a recusa por PII não seja mascarada por
    # um erro de configuração do backend.
    agent = DiscoveryAgent(llm_factory=get_llm_client)
    report = agent.run(engine, cfg.source.dialect, cfg.scope_tables or None)
    llm = agent.llm

    out_dir = _output_dir(cfg.project)
    out = out_dir / "discovery_report.json"
    out.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    pii_columns = discovery_pii_columns(report)
    _record_run(
        cfg.project,
        "discovery",
        llm,
        started,
        started_at=started_at,
        artifact=out.name,
        tables=len(report.tables),
        pii_columns=pii_columns,
    )
    _record_audit(
        cfg.project,
        "discovery",
        started,
        started_at,
        llm=llm,
        pii_columns=pii_columns,
        artifacts=hash_artifacts(out_dir, [out.name]),
    )
    return out


def run_mapping(client_name: str, on_table: ProgressCallback | None = None) -> Path:
    cfg = load_project_config(client_name)
    started = time.perf_counter()
    started_at = datetime.now(UTC)
    out_dir = _output_dir(cfg.project)

    discovery_path = out_dir / "discovery_report.json"
    discovery = DiscoveryReport.model_validate_json(discovery_path.read_text())

    # A PII do relatório é decidida antes de qualquer chamada: o Agente 2 lê o
    # relatório do disco, então ele é mascarado e a política é conferida aqui,
    # não só no `discover`.
    pii_columns = discovery_pii_columns(discovery)
    discovery = sanitize_discovery(discovery)
    enforce_pii_backend_policy(bool(pii_columns))

    llm = get_llm_client()
    target_schema = TargetSchema.load(cfg.target_schema_path)

    mapping = MappingAgent(llm).run(
        cfg.project, discovery, target_schema, on_table=on_table
    )

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
        started_at=started_at,
        artifact=review_xlsx.name,
        target_tables=len(target_schema.tables),
        target_schema_version=target_schema.version,
        entries=len(mapping.entries),
        unmapped=len(mapping.unmapped_target_fields),
        warnings=len(mapping.warnings),
        pii_columns=pii_columns,
    )
    _record_audit(
        cfg.project,
        "mapping",
        started,
        started_at,
        llm=llm,
        pii_columns=pii_columns,
        target_schema={"version": target_schema.version},
        mapping={
            "id": mapping.mapping_id,
            "sha256": file_sha256(mapping_json),
            "entries": len(mapping.entries),
        },
        artifacts=hash_artifacts(out_dir, [mapping_json.name, review_xlsx.name]),
    )
    return review_xlsx


def run_etl_generation(
    client_name: str,
    *,
    dry_run: bool = False,
    force: bool = False,
    approved_by: str | None = None,
) -> Path:
    """Só deve ser chamado depois que o analista revisou e salvou a planilha
    (mapeamento_para_revisao.xlsx) com a coluna `status` atualizada."""
    cfg = load_project_config(client_name)
    started = time.perf_counter()
    started_at = datetime.now(UTC)
    out_dir = _output_dir(cfg.project)

    approval = {} if force else verify_approval(out_dir)

    mapping = MappingDocument.model_validate_json((out_dir / "mapping.json").read_text())
    mapping = load_reviewed_xlsx(mapping, out_dir / "mapeamento_para_revisao.xlsx")
    target_schema = TargetSchema.load(cfg.target_schema_path)

    # Versão do schema: a mesma que o mapeamento viu? (`force` registra o
    # desvio na auditoria em vez de escondê-lo.)
    compatibility = check_schema_compatibility(
        mapping.target_schema_version,
        target_schema.version,
        approval,
        force=force,
    )

    discovery = _load_discovery(out_dir)
    pii_columns = discovery_pii_columns(discovery)

    result = ETLAgent().run(
        mapping,
        target_schema=target_schema,
        source_dialect=cfg.source.dialect,
        target_dialect=cfg.target.dialect,
        output_dir=out_dir,
        source_schema=cfg.source.schema_name,
        target_schema_name=cfg.target.schema_name,
        discovery=discovery,
        etl=cfg.etl,
        dry_run=dry_run,
    )

    fingerprint = review_fingerprint(mapping)
    approver = resolve_approver(approved_by)
    mapping_sha256 = approval.get("mapping_sha256") or file_sha256(out_dir / "mapping.json")

    artifacts = sorted(path.name for path in result.written.values())
    if not dry_run:
        _append_audit(
            out_dir / "relatorio.md",
            approval,
            fingerprint,
            compatibility=compatibility,
            approver=approver,
        )

    counts = {status.value: 0 for status in ReviewStatus}
    for entry in mapping.entries:
        counts[entry.status.value] += 1

    _record_run(
        cfg.project,
        "generate",
        None,
        started,
        started_at=started_at,
        dry_run=dry_run,
        entries=result.plan.entry_count,
        approved=counts[ReviewStatus.approved.value] + counts[ReviewStatus.edited.value],
        tables=[step.table for step in result.plan.steps],
        skipped=len(result.plan.skipped),
        warnings=len(result.plan.warnings),
        mapping_sha256=mapping_sha256,
        review_sha256=fingerprint,
        target_schema=compatibility,
        approved_by=approver,
        pii_columns=pii_columns,
        artifacts=artifacts,
    )
    _record_audit(
        cfg.project,
        "generate",
        started,
        started_at,
        pii_columns=pii_columns,
        dry_run=dry_run,
        force=force,
        approver=approver,
        approval={
            "mapping_id": approval.get("mapping_id"),
            "mapping_sha256": mapping_sha256,
            "exported_at": approval.get("created_at"),
        },
        target_schema=compatibility,
        mapping={
            "id": mapping.mapping_id,
            "sha256": file_sha256(out_dir / "mapping.json"),
            "entries": len(mapping.entries),
        },
        review={"sha256": fingerprint, **counts},
        artifacts=hash_artifacts(out_dir, artifacts),
    )

    return result.written["etl_plan.json"] if dry_run else out_dir / "etl_script.sql"


def _load_discovery(out_dir: Path) -> DiscoveryReport | None:
    path = out_dir / "discovery_report.json"
    if not path.exists():
        return None
    return DiscoveryReport.model_validate_json(path.read_text())


def _append_audit(
    report: Path,
    approval: dict,
    fingerprint: str,
    *,
    compatibility: dict | None = None,
    approver: str | None = None,
) -> None:
    compatibility = compatibility or {}
    mapping_version = compatibility.get("mapping_version") or "desconhecida"
    schema_version = compatibility.get("schema_version") or approval.get(
        "target_schema_version", "desconhecida"
    )
    if compatibility.get("compatible") is False:
        schema_line = (
            f"- target schema: `{schema_version}` — **INCOMPATÍVEL** "
            f"com `{mapping_version}` (force)"
        )
    elif compatibility.get("compatible") is None:
        schema_line = (
            f"- target schema: `{schema_version}` — compatibilidade não "
            "verificável (mapeamento sem versão registrada)"
        )
    else:
        schema_line = f"- target schema: `{schema_version}` (compatível com o mapeamento)"

    lines = [
        "",
        "## Auditoria",
        "",
        f"- aprovador: `{approver or 'não informado'}`",
        f"- `mapping.json` (sha256): `{approval.get('mapping_sha256', 'não verificado')}`",
        f"- estado revisado (sha256): `{fingerprint}`",
        schema_line,
    ]
    with report.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
