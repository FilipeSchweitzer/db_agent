"""Fase 5 — cobertura da CLI de avaliação (`evaluate` e `eval-init`).

Os testes de `test_evaluation.py` exercitam o harness pela API; estes cobrem
o caminho que o usuário realmente digita: resolução do caso, opções,
mensagens de erro limpas (sem traceback) e o rascunho criado por `eval-init`.
"""
from __future__ import annotations

from types import SimpleNamespace

import yaml
from typer.testing import CliRunner

from app.main import app as cli
from tests.conftest import make_discovery, make_schema
from tests.test_evaluation import _document, _write_case

runner = CliRunner()


def test_cli_evaluate_offline_gera_relatorio(tmp_path):
    case = _write_case(tmp_path / "caso")
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(_document().model_dump_json(indent=2), encoding="utf-8")
    output = tmp_path / "saida"

    result = runner.invoke(
        cli,
        [
            "evaluate",
            str(case),
            "--mapping",
            str(mapping_path),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert (output / "eval_report.json").exists()
    assert (output / "eval_report.md").exists()
    assert "offline" in result.stdout


def test_cli_evaluate_recusa_repeats_com_mapping(tmp_path):
    case = _write_case(tmp_path / "caso")
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(_document().model_dump_json(indent=2), encoding="utf-8")

    result = runner.invoke(
        cli,
        [
            "evaluate",
            str(case),
            "--mapping",
            str(mapping_path),
            "--repeats",
            "2",
        ],
    )

    assert result.exit_code != 0
    assert "só se aplica ao modo live" in result.output


def test_cli_evaluate_caso_corrompido_vira_erro_limpo(tmp_path):
    case = _write_case(tmp_path / "caso")
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(_document().model_dump_json(indent=2), encoding="utf-8")
    (case / "discovery_report.json").write_text("{not json", encoding="utf-8")

    result = runner.invoke(
        cli, ["evaluate", str(case), "--mapping", str(mapping_path)]
    )

    assert result.exit_code == 2
    assert "Caso inválido" in result.output
    assert "Traceback" not in result.output


def test_cli_eval_init_cria_rascunho(tmp_path, monkeypatch):
    schema_path = tmp_path / "schema.yaml"
    schema_path.write_text(
        yaml.safe_dump(make_schema().model_dump(), sort_keys=False),
        encoding="utf-8",
    )
    out_dir = tmp_path / "out" / "projeto"
    out_dir.mkdir(parents=True)
    (out_dir / "discovery_report.json").write_text(
        make_discovery().model_dump_json(indent=2), encoding="utf-8"
    )
    (out_dir / "mapping.json").write_text(
        _document().model_dump_json(indent=2), encoding="utf-8"
    )
    cfg = SimpleNamespace(project="projeto", target_schema_path=schema_path)

    monkeypatch.setattr("app.main.load_project_config", lambda name: cfg)
    monkeypatch.setattr("app.main.orchestrator.OUTPUT_ROOT", tmp_path / "out")
    monkeypatch.setattr("app.main.EVAL_ROOT", tmp_path / "evalroot")

    result = runner.invoke(
        cli, ["eval-init", "novo-caso", "--project", "projeto"]
    )

    assert result.exit_code == 0
    assert (tmp_path / "evalroot" / "novo-caso" / "golden.yaml").exists()
    assert (tmp_path / "evalroot" / "novo-caso" / "target_schema.yaml").exists()
