from __future__ import annotations

import json
from pathlib import Path

from app.agents.agent1_discovery import DiscoveryAgent
from app.agents.agent2_mapping import MappingAgent
from app.agents.agent3_etl import ETLAgent
from app.config import ProjectConfig, load_project_config
from app.db.connectors import get_engine
from app.llm import get_llm_client
from app.review.export import export_to_xlsx, load_reviewed_xlsx
from app.schemas.mapping import MappingDocument
from app.schemas.target_schema import TargetSchema

OUTPUT_ROOT = Path("/app/output")


def _output_dir(project: str) -> Path:
    d = OUTPUT_ROOT / project
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_discovery(client_name: str) -> Path:
    cfg = load_project_config(client_name)
    engine = get_engine(cfg.source, read_only=True)
    llm = get_llm_client()

    report = DiscoveryAgent(llm).run(engine, cfg.source.dialect, cfg.scope_tables or None)

    out = _output_dir(cfg.project) / "discovery_report.json"
    out.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return out


def run_mapping(client_name: str) -> Path:
    cfg = load_project_config(client_name)
    llm = get_llm_client()

    discovery_path = _output_dir(cfg.project) / "discovery_report.json"
    from app.schemas.profiling import DiscoveryReport
    discovery = DiscoveryReport.model_validate_json(discovery_path.read_text())

    target_schema = TargetSchema.load(cfg.target_schema_path)

    mapping = MappingAgent(llm).run(cfg.project, discovery, target_schema)

    mapping_json = _output_dir(cfg.project) / "mapping.json"
    mapping_json.write_text(mapping.model_dump_json(indent=2), encoding="utf-8")

    review_xlsx = _output_dir(cfg.project) / "mapeamento_para_revisao.xlsx"
    export_to_xlsx(mapping, review_xlsx)
    return review_xlsx


def run_etl_generation(client_name: str) -> Path:
    """Só deve ser chamado depois que o analista revisou e salvou a planilha
    (mapeamento_para_revisao.xlsx) com a coluna `status` atualizada."""
    cfg = load_project_config(client_name)

    out_dir = _output_dir(cfg.project)
    mapping_json = out_dir / "mapping.json"
    mapping = MappingDocument.model_validate_json(mapping_json.read_text())

    reviewed_xlsx = out_dir / "mapeamento_para_revisao.xlsx"
    mapping = load_reviewed_xlsx(mapping, reviewed_xlsx)

    ETLAgent().run(
        mapping,
        source_dialect=cfg.source.dialect,
        target_dialect=cfg.target.dialect,
        output_dir=out_dir,
    )
    return out_dir / "etl_script.sql"
