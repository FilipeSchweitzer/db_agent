"""Avaliação (Fase 5): mede o agente contra um mapeamento manual congelado.

- `golden` — carrega/valida o caso (`golden.yaml` + schema + discovery) e cria
  um rascunho a partir de um `map` já executado;
- `metrics` — precisão/recall/F1 de mapeamento, acurácia de tipo e PII;
- `runner` — executa offline (artefato) ou live (agente), com repetições;
- o relatório sai em `eval_report.json` + `eval_report.md`.
"""
from __future__ import annotations

from app.evaluation.golden import (
    EVAL_ROOT,
    GoldenCase,
    GoldenError,
    LoadedCase,
    load_case,
    load_discovery_report,
    resolve_case_ref,
    scaffold_case,
)
from app.evaluation.runner import evaluate_case, render_markdown, write_reports

__all__ = [
    "EVAL_ROOT",
    "GoldenCase",
    "GoldenError",
    "LoadedCase",
    "evaluate_case",
    "load_case",
    "load_discovery_report",
    "render_markdown",
    "resolve_case_ref",
    "scaffold_case",
    "write_reports",
]
