from __future__ import annotations

import json
import time

import pytest
import yaml

from app import config as app_config
from app import orchestrator
from app.review.approval import ApprovalError, write_approval
from app.review.export import export_to_xlsx
from support import ScriptedClient
from tests.conftest import make_mapping, make_schema

PROJECT_NAME = "test-client"


def test_run_log_acumula_fases(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "OUTPUT_ROOT", tmp_path)
    llm = ScriptedClient([])
    started = time.perf_counter()

    orchestrator._record_run("proj", "discovery", None, started, tables=2)
    orchestrator._record_run("proj", "mapping", llm, started, entries=5)

    log = json.loads((tmp_path / "proj" / "run_log.json").read_text(encoding="utf-8"))
    fases = [run["phase"] for run in log["runs"]]

    assert fases == ["discovery", "mapping"]
    assert log["runs"][0]["tables"] == 2
    assert log["runs"][0]["llm"] is None
    assert log["runs"][1]["llm"]["model"] == "modelo-de-teste"
    assert log["runs"][1]["llm"]["totals"]["attempts"] == 0
    assert log["runs"][1]["entries"] == 5


def test_run_log_cria_diretorio_do_projeto(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "OUTPUT_ROOT", tmp_path)

    caminho = orchestrator._record_run("novo", "discovery", None, time.perf_counter())

    assert caminho.exists()
    assert caminho.parent.name == "novo"


# ----------------------------------------------------------------------
# generate: aprovação, dry-run e artefatos
# ----------------------------------------------------------------------
@pytest.fixture
def projeto(tmp_path, monkeypatch):
    """Config do cliente + saída com mapping.json, planilha e approval."""
    config_root = tmp_path / "config"
    project_dir = config_root / PROJECT_NAME
    project_dir.mkdir(parents=True)

    (project_dir / "target_schema.yaml").write_text(
        yaml.safe_dump(make_schema().model_dump(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    (project_dir / "project.yaml").write_text(
        yaml.safe_dump(
            {
                "project": PROJECT_NAME,
                "source": {
                    "dialect": "postgres",
                    "host": "origem",
                    "port": 5432,
                    "database": "legado",
                    "user": "leitor",
                    "schema_name": "legado",
                },
                "target": {
                    "dialect": "postgres",
                    "host": "destino",
                    "port": 5432,
                    "database": "gestao",
                    "user": "migrador",
                    "schema_name": "public",
                },
                "target_schema_file": "target_schema.yaml",
                "etl": {"sequence_pattern": "seq_{table}"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(app_config, "CONFIG_ROOT", config_root)
    monkeypatch.setattr(orchestrator, "OUTPUT_ROOT", tmp_path / "output")

    out = tmp_path / "output" / PROJECT_NAME
    out.mkdir(parents=True)
    mapping = make_mapping()
    (out / "mapping.json").write_text(
        mapping.model_dump_json(indent=2), encoding="utf-8"
    )
    export_to_xlsx(mapping, out / "mapeamento_para_revisao.xlsx")
    write_approval(
        out,
        project=mapping.project,
        mapping_id=mapping.mapping_id,
        target_schema_version="test.1",
        entries=len(mapping.entries),
    )
    return out


def test_generate_dry_run_nao_escreve_sql(projeto):
    caminho = orchestrator.run_etl_generation(PROJECT_NAME, dry_run=True)

    assert caminho.name == "etl_plan.json"
    assert not (projeto / "etl_script.sql").exists()
    assert not (projeto / "etl_schema.sql").exists()


def test_generate_escreve_os_quatro_scripts(projeto):
    caminho = orchestrator.run_etl_generation(PROJECT_NAME)

    assert caminho.name == "etl_script.sql"
    for name in (
        "etl_schema.sql",
        "etl_script.sql",
        "etl_constraints.sql",
        "etl_validation.sql",
        "etl_plan.json",
        "relatorio.md",
    ):
        assert (projeto / name).exists(), name


def test_generate_registra_auditoria_no_relatorio(projeto):
    orchestrator.run_etl_generation(PROJECT_NAME)

    report = (projeto / "relatorio.md").read_text(encoding="utf-8")
    assert "## Auditoria" in report
    assert "mapping.json` (sha256)" in report
    assert "estado revisado (sha256)" in report


def test_generate_recusa_mapping_regerado(projeto):
    mapping_file = projeto / "mapping.json"
    original = mapping_file.read_text(encoding="utf-8")
    mapping_file.write_text(original.replace("COD_MUN", "COD_MUNICIPIO"), encoding="utf-8")

    with pytest.raises(ApprovalError, match="mudou desde que a planilha"):
        orchestrator.run_etl_generation(PROJECT_NAME)

    assert not (projeto / "etl_script.sql").exists()


def test_force_ignora_a_divergencia_de_hash(projeto):
    mapping_file = projeto / "mapping.json"
    mapping_file.write_text(
        mapping_file.read_text(encoding="utf-8").replace("COD_MUN", "COD_MUNICIPIO"),
        encoding="utf-8",
    )

    orchestrator.run_etl_generation(PROJECT_NAME, force=True)

    assert (projeto / "etl_script.sql").exists()


def test_generate_registra_secoes_no_run_log(projeto):
    orchestrator.run_etl_generation(PROJECT_NAME, dry_run=True)

    log = json.loads((projeto / "run_log.json").read_text(encoding="utf-8"))
    entry = log["runs"][-1]
    assert entry["phase"] == "generate"
    assert entry["dry_run"] is True
    assert entry["tables"] == ["municipio", "bairro", "pessoa"]
    assert entry["mapping_sha256"]
    assert entry["review_sha256"]
    assert entry["artifacts"] == ["etl_plan.json"]
