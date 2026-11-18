from __future__ import annotations

import json
import time

from app import orchestrator
from support import ScriptedClient


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
