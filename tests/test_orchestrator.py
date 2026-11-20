from __future__ import annotations

import json
import time

import pytest
import yaml

from app import config as app_config
from app import orchestrator
from app.db import introspection as tools
from app.privacy.policy import PIIPolicyError
from app.review.approval import ApprovalError, write_approval
from app.review.export import export_to_xlsx
from app.schemas.profiling import ColumnProfile, DiscoveryReport, TableProfile
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


# ----------------------------------------------------------------------
# Fase 4: auditoria.json, versão do schema e PII
# ----------------------------------------------------------------------
def _schema_path(projeto):
    return projeto.parents[1] / "config" / PROJECT_NAME / "target_schema.yaml"


def _escrever_discovery(projeto, report: DiscoveryReport) -> None:
    (projeto / "discovery_report.json").write_text(
        report.model_dump_json(indent=2), encoding="utf-8"
    )


def _discovery_com_pii() -> DiscoveryReport:
    return DiscoveryReport(
        source_dialect="postgres",
        tables=[
            TableProfile(
                table="CAD_PESSOA",
                row_count=10,
                columns=[
                    ColumnProfile(
                        name="NUM_DOC",
                        inferred_type="string",
                        null_rate=0.0,
                        top_values=["123.456.789-00"],
                        notes="documento 123.456.789-00",
                    )
                ],
            )
        ],
    )


def test_generate_grava_auditoria_por_execucao(projeto):
    orchestrator.run_etl_generation(PROJECT_NAME, approved_by="maria.silva")

    data = json.loads((projeto / "auditoria.json").read_text(encoding="utf-8"))
    record = data["executions"][-1]

    assert data["schema_version"] == 1
    assert record["phase"] == "generate"
    assert record["run_id"]
    assert record["approver"] == "maria.silva"
    assert record["actor"]["user"]
    assert record["mapping"]["sha256"]
    assert record["review"]["sha256"]
    assert record["review"]["approved"] > 0
    assert record["target_schema"]["schema_version"] == "test.1"
    assert record["target_schema"]["compatible"] is True
    assert "etl_script.sql" in record["artifacts"]
    assert "relatorio.md" in record["artifacts"]
    assert record["pii"]["masked"] is True
    assert record["pii"]["detected"] is False


def test_generate_dry_run_audita_sem_artefatos_executaveis(projeto):
    orchestrator.run_etl_generation(PROJECT_NAME, dry_run=True)

    record = json.loads((projeto / "auditoria.json").read_text())["executions"][-1]

    assert record["dry_run"] is True
    assert record["artifacts"] == {"etl_plan.json": record["artifacts"]["etl_plan.json"]}


def test_generate_recusa_schema_de_versao_diferente(projeto):
    schema_path = _schema_path(projeto)
    schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    schema["version"] = "test.2"
    schema_path.write_text(yaml.safe_dump(schema, sort_keys=False), encoding="utf-8")

    with pytest.raises(ApprovalError, match="target_schema.yaml mudou"):
        orchestrator.run_etl_generation(PROJECT_NAME)

    assert not (projeto / "etl_script.sql").exists()


def test_force_gera_e_audita_o_schema_incompativel(projeto):
    schema_path = _schema_path(projeto)
    schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    schema["version"] = "test.2"
    schema_path.write_text(yaml.safe_dump(schema, sort_keys=False), encoding="utf-8")

    orchestrator.run_etl_generation(PROJECT_NAME, force=True)

    record = json.loads((projeto / "auditoria.json").read_text())["executions"][-1]
    assert record["target_schema"]["compatible"] is False
    assert record["force"] is True


def test_discover_grava_auditoria_e_relatorio_mascarado(projeto, monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "local")
    monkeypatch.setattr(orchestrator, "get_engine", lambda *args, **kwargs: object())
    monkeypatch.setattr(tools, "list_tables", lambda engine: ["CAD_PESSOA"])
    monkeypatch.setattr(
        tools, "get_schema", lambda engine, table: [{"name": "CPF", "type": "VARCHAR(14)"}]
    )
    monkeypatch.setattr(
        tools,
        "profile_column",
        lambda engine, table, column: {
            "table": table,
            "column": column,
            "row_count": 10,
            "null_rate": 0.0,
            "distinct_count": 2,
            "top_values": ["123.456.789-00"],
        },
    )
    client = ScriptedClient(
        [
            json.dumps(
                {
                    "table": "CAD_PESSOA",
                    "row_count": 10,
                    "columns": [
                        {
                            "name": "CPF",
                            "inferred_type": "string",
                            "null_rate": 0.0,
                            "top_values": ["###.###.###-##"],
                            "pii": True,
                        }
                    ],
                    "risks": [],
                }
            )
        ]
    )
    monkeypatch.setattr(orchestrator, "get_llm_client", lambda: client)

    path = orchestrator.run_discovery(PROJECT_NAME)

    report = json.loads(path.read_text(encoding="utf-8"))
    assert "123.456.789-00" not in path.read_text(encoding="utf-8")
    assert report["pii_columns"] == ["CAD_PESSOA.CPF"]

    record = json.loads((projeto / "auditoria.json").read_text())["executions"][-1]
    assert record["phase"] == "discovery"
    assert record["pii"]["detected"] is True
    assert record["pii"]["columns"] == ["CAD_PESSOA.CPF"]
    assert record["artifacts"]["discovery_report.json"]


def test_map_exige_backend_local_quando_ha_pii(projeto, monkeypatch):
    _escrever_discovery(projeto, _discovery_com_pii())
    monkeypatch.setenv("LLM_BACKEND", "openai")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)

    with pytest.raises(PIIPolicyError, match="sai da rede"):
        orchestrator.run_mapping(PROJECT_NAME)


def test_map_com_backend_local_mascara_e_registra_a_versao(projeto, monkeypatch):
    _escrever_discovery(projeto, _discovery_com_pii())
    monkeypatch.setenv("LLM_BACKEND", "local")
    client = ScriptedClient(
        [json.dumps({"entries": [], "unmapped_target_fields": []})] * 4
    )
    monkeypatch.setattr(orchestrator, "get_llm_client", lambda: client)

    orchestrator.run_mapping(PROJECT_NAME)

    mapping = json.loads((projeto / "mapping.json").read_text(encoding="utf-8"))
    assert mapping["target_schema_version"] == "test.1"
    assert "123.456.789-00" not in " ".join(client.prompts)

    record = json.loads((projeto / "auditoria.json").read_text())["executions"][-1]
    assert record["phase"] == "mapping"
    assert record["pii"]["detected"] is True
    assert record["pii"]["columns"] == ["CAD_PESSOA.NUM_DOC"]
    assert record["pii"]["llm_local"] is True
