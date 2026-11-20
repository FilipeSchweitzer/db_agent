"""Executa um caso golden: modo offline (artefato existente) ou live (agente).

Offline é o modo de custo zero e o que roda no CI: compara um `mapping.json`
já gerado com o golden. Live roda o Agente 2 contra o discovery congelado e,
com `--repeats`, mede se a resposta é estável entre execuções.

Nos dois casos o discovery vem do caso, nunca do banco: a avaliação mede o
prompt/agente, não a variação do legado.
"""
from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime
from pathlib import Path

from app.agents.agent2_mapping import MappingAgent, ProgressCallback
from app.evaluation.golden import LoadedCase
from app.evaluation.metrics import (
    evaluate_discovery,
    evaluate_mapping,
    normalize_name,
    prediction_map,
)
from app.privacy.report import sanitize_discovery
from app.schemas.mapping import MappingDocument
from app.schemas.profiling import DiscoveryReport

REPORT_SCHEMA_VERSION = 1

# Métricas que ganham média/desvio quando há mais de uma execução.
_SUMMARY_KEYS = (
    "precision",
    "recall",
    "f1",
    "exact_accuracy",
    "unmapped_accuracy",
    "transformation_accuracy",
)


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4)


def _spread(values: list[float]) -> dict:
    return {
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "stdev": round(statistics.pstdev(values), 4),
    }


def _metric(run: dict, key: str) -> float | None:
    result = run["metrics"]
    if key == "unmapped_accuracy":
        return result["metrics"]["unmapped"]["accuracy"]
    if key == "transformation_accuracy":
        return result["transformations"]["accuracy"]
    return result["metrics"].get(key)


def _summary(runs: list[dict]) -> tuple[dict, dict]:
    means: dict[str, float | None] = {}
    spread: dict[str, dict] = {}
    for key in _SUMMARY_KEYS:
        present = [
            value for value in (_metric(run, key) for run in runs) if value is not None
        ]
        means[key] = _mean(present) if present else None
        if len(present) > 1:
            spread[key] = _spread(present)
    return means, spread


def _consistency(golden_keys: list[tuple[str, str]], documents: list[MappingDocument]) -> dict:
    """Fração de campos em que todas as execuções propuseram a mesma origem."""
    if len(documents) <= 1:
        return {"evaluated": False, "agreement": None, "unstable": []}

    maps = [prediction_map(document) for document in documents]
    unstable: list[str] = []
    for key in golden_keys:
        predictions = {item.get(key) for item in maps}
        if len(predictions) > 1:
            unstable.append(f"{key[0]}.{key[1]}")
    agreement = round(1 - (len(unstable) / len(golden_keys)), 4) if golden_keys else None
    return {"evaluated": True, "agreement": agreement, "unstable": sorted(unstable)}


def evaluate_case(
    case: LoadedCase,
    *,
    mapping: MappingDocument | None = None,
    llm=None,
    repeats: int = 1,
    discovery: DiscoveryReport | None = None,
    on_table: ProgressCallback | None = None,
) -> dict:
    """Avalia um caso e devolve o relatório completo (sem escrever arquivo)."""
    if repeats < 1:
        raise ValueError("repeats precisa ser >= 1")
    if mapping is None and llm is None:
        raise ValueError("informe mapping=... (offline) ou llm=... (live)")

    report_discovery = discovery or case.discovery
    documents: list[MappingDocument] = []
    mode: str

    if mapping is not None:
        mode = "offline"
        documents.append(mapping)
    else:
        mode = "live"
        # O agente nunca vê valor real, nem no modo de avaliação.
        safe_discovery = sanitize_discovery(report_discovery)
        for _ in range(repeats):
            documents.append(
                MappingAgent(llm).run(
                    case.golden.case,
                    safe_discovery,
                    case.target_schema,
                    on_table=on_table,
                )
            )

    runs = [
        {
            "index": index,
            "mapping_id": document.mapping_id,
            "warnings": document.warnings,
            "metrics": evaluate_mapping(case.golden.mapping, document),
        }
        for index, document in enumerate(documents, start=1)
    ]
    summary, spread = _summary(runs)
    golden_keys = [
        (normalize_name(row.target_table), normalize_name(row.target_field))
        for row in case.golden.mapping
    ]

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "case": case.golden.case,
        "description": case.golden.description,
        "synthetic": case.golden.synthetic,
        "evaluated_at": datetime.now(UTC).isoformat(),
        "mode": mode,
        "repeats": len(documents),
        "warnings": list(case.warnings),
        "mapping": {
            "summary": summary,
            "spread": spread,
            "consistency": _consistency(golden_keys, documents),
            "runs": runs,
        },
        "discovery": evaluate_discovery(case.golden.discovery, report_discovery),
    }


def _rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def render_markdown(report: dict) -> str:
    mode = "offline (artefato existente)" if report["mode"] == "offline" else "live"
    lines = [
        f"# Avaliação — {report['case']}",
        "",
        f"- modo: **{mode}**",
        f"- execuções: {report['repeats']}",
        f"- avaliado em: {report['evaluated_at']}",
    ]
    if report["synthetic"]:
        lines.append("- ⚠️ caso **sintético**: serve para exercitar o formato, não como benchmark")
    if report["description"]:
        lines.extend(["", report["description"]])

    lines.extend(["", "## Mapeamento", "", "| métrica | valor |", "|---|---|"])
    summary = report["mapping"]["summary"]
    labels = [
        ("precision", "Precisão (campos mapeados)"),
        ("recall", "Recall (campos mapeados)"),
        ("f1", "F1"),
        ("exact_accuracy", "Acerto exato (inclui unmapped)"),
        ("unmapped_accuracy", "Acerto em deixar sem origem"),
        ("transformation_accuracy", "Acerto de transformação"),
    ]
    for key, label in labels:
        value = _rate(summary.get(key))
        spread = report["mapping"]["spread"].get(key)
        if spread:
            value += f" (desvio {spread['stdev'] * 100:.1f} p.p.)"
        lines.append(f"| {label} | {value} |")

    consistency = report["mapping"]["consistency"]
    if consistency.get("evaluated"):
        lines.append(
            f"| Consistência entre execuções | {_rate(consistency['agreement'])} |"
        )
        if consistency.get("unstable"):
            lines.extend(
                ["", "Campos que mudaram entre execuções: "]
                + [f"- `{item}`" for item in consistency["unstable"]]
            )

    lines.extend(
        [
            "",
            "### Por tabela de destino",
            "",
            "| tabela | precisão | recall | F1 | erros |",
            "|---|---|---|---|---|",
        ]
    )
    for row in report["mapping"]["runs"][0]["metrics"]["by_table"]:
        lines.append(
            f"| `{row['table']}` | {_rate(row['precision'])} | {_rate(row['recall'])} "
            f"| {_rate(row['f1'])} | {row['errors']} |"
        )

    errors = report["mapping"]["runs"][0]["metrics"]["errors"]
    lines.extend(["", f"### Erros de mapeamento ({len(errors)})", ""])
    if not errors:
        lines.append("Nenhum.")
    else:
        for error in errors[:40]:
            expected = f"`{error['expected']}`" if error["expected"] else "*(sem origem)*"
            predicted = f"`{error['predicted']}`" if error["predicted"] else "*(sem origem)*"
            lines.append(
                f"- `{error['target']}` — esperado {expected}, previsto {predicted} "
                f"({error['label']})"
            )
        if len(errors) > 40:
            lines.append(f"- … e mais {len(errors) - 40} erro(s); ver o JSON.")

    transformations = report["mapping"]["runs"][0]["metrics"]["transformations"]
    lines.extend(["", f"### Transformações ({transformations['evaluated']} avaliadas)", ""])
    if transformations["evaluated"] == 0:
        lines.append(
            "Nenhuma expectativa de transformação no golden (chave `transformation`)."
        )
    else:
        lines.append(f"Acerto: {_rate(transformations['accuracy'])}")
        for error in transformations["errors"][:20]:
            lines.append(
                f"- `{error['target']}` — esperado `{error['expected'] or 'nenhuma'}`, "
                f"previsto `{error['predicted'] or 'nenhuma'}`"
            )

    discovery = report["discovery"]
    lines.extend(
        [
            "",
            "## Discovery",
            "",
            f"- colunas esperadas: {discovery['columns_expected']} "
            f"(encontradas: {discovery['columns_found']})",
            f"- acurácia de tipo: {_rate(discovery['type']['accuracy'])} "
            f"({discovery['type']['correct']}/{discovery['type']['evaluated']})",
            f"- PII: precisão {_rate(discovery['pii']['precision'])}, "
            f"recall {_rate(discovery['pii']['recall'])}",
        ]
    )
    if discovery["missing"]:
        lines.extend(
            ["", "Colunas do golden ausentes no relatório:"]
            + [f"- `{item}`" for item in discovery["missing"]]
        )
    if discovery["pii"]["false_negatives"]:
        lines.extend(
            [
                "",
                "### ⚠️ PII não marcada (falso negativo)",
                "",
                "O erro caro: a coluna é pessoal e o discovery deixou passar.",
            ]
            + [
                f"- `{item['column']}` — {item['note']}"
                for item in discovery["pii"]["false_negatives"]
            ]
        )
    if discovery["type"]["errors"]:
        lines.extend(
            ["", "Erros de tipo:"]
            + [
                f"- `{item['column']}` — esperado `{item['expected']}`, "
                f"previsto `{item['predicted'] or 'ausente'}`"
                for item in discovery["type"]["errors"][:20]
            ]
        )

    warnings = list(
        dict.fromkeys(
            report["warnings"]
            + [
                warning
                for run in report["mapping"]["runs"]
                for warning in run["warnings"]
            ]
        )
    )
    if warnings:
        lines.extend(["", "## Avisos", ""])
        lines.extend(f"- {warning}" for warning in warnings)

    return "\n".join(lines) + "\n"


def write_reports(report: dict, output_dir) -> tuple[Path, Path]:
    """Escreve `eval_report.json` e `eval_report.md` e devolve os caminhos."""
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / "eval_report.json"
    md_path = destination / "eval_report.md"
    json_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path
