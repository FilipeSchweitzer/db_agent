"""Agente 3: recorte de aprovação, artefatos em disco e dry-run.

O dry-run é o que permite rodar a geração em produção sem deixar um script
executável na pasta por engano — por isso ele escreve SÓ o plano.
"""
from __future__ import annotations

import json

from app.agents.agent3_etl import PLAN_FILE, ETLAgent
from tests.conftest import make_discovery, make_mapping, make_schema


def _run(tmp_path, *, dry_run=False, mapping=None):
    return ETLAgent().run(
        mapping or make_mapping(),
        target_schema=make_schema(),
        source_dialect="postgres",
        target_dialect="postgres",
        output_dir=tmp_path,
        source_schema="legado",
        target_schema_name="public",
        discovery=make_discovery(),
        dry_run=dry_run,
    )


def test_dry_run_escreve_apenas_o_plano(tmp_path):
    result = _run(tmp_path, dry_run=True)

    assert result.dry_run is True
    assert sorted(path.name for path in tmp_path.iterdir()) == [PLAN_FILE]
    assert result.written[PLAN_FILE].exists()


def test_dry_run_reporta_o_que_seria_gerado(tmp_path):
    result = _run(tmp_path, dry_run=True)

    assert [step.table for step in result.plan.steps] == [
        "municipio",
        "bairro",
        "pessoa",
    ]
    artifact = json.loads((tmp_path / PLAN_FILE).read_text())
    assert artifact["target_dialect"] == "postgres"
    assert len(artifact["tables"]) == 3


def test_execucao_real_escreve_todos_os_artefatos(tmp_path):
    result = _run(tmp_path)

    assert result.dry_run is False
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [
            PLAN_FILE,
            "etl_schema.sql",
            "etl_script.sql",
            "etl_constraints.sql",
            "etl_validation.sql",
            "relatorio.md",
        ]
    )
    assert set(result.written) == {path.name for path in tmp_path.iterdir()}


def test_todo_arquivo_termina_com_quebra_de_linha(tmp_path):
    _run(tmp_path)
    for path in tmp_path.iterdir():
        assert path.read_text(encoding="utf-8").endswith("\n"), path.name


def test_gerar_duas_vezes_sobrescreve_sem_duplicar(tmp_path):
    first = _run(tmp_path)
    second = _run(tmp_path)
    assert first.written.keys() == second.written.keys()
    assert len(list(tmp_path.iterdir())) == len(first.written)


def test_dry_run_nao_deixa_lixo_de_execucao_anterior(tmp_path):
    """Rodar o dry-run depois de um generate não apaga os SQL já gerados."""
    _run(tmp_path)
    _run(tmp_path, dry_run=True)
    assert (tmp_path / "etl_script.sql").exists()
