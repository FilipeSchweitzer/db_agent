"""Métricas puras da avaliação — sem I/O, sem LLM, sem banco.

Duas perguntas, dois conjuntos de números:

- **mapeamento**: para cada campo do destino, o Agente 2 escolheu a mesma
  origem que o mapeamento manual? Precisão/recall/F1 tratam "mapeou errado"
  como erro duplo (falso positivo e falso negativo), que é o que dói na
  revisão.
- **discovery**: o tipo inferido bate (por família) e a flag de PII acerta?
  Falso negativo de PII é listado separadamente porque é o erro caro.

`precision`/`recall`/`f1` são `None` quando o denominador é zero — melhor
"não aplicável" do que um 1.0 que esconde que não havia o que medir.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.schemas.mapping import MappingDocument, MappingEntry
from app.schemas.profiling import ColumnProfile, DiscoveryReport

if TYPE_CHECKING:  # evita ciclo: golden.py importa normalize_name daqui
    from app.evaluation.golden import GoldenColumn, GoldenMapping

# Famílias de tipo. O Agente 1 devolve texto livre (`varchar`, `int`,
# `timestamp`); comparar família evita punir sinônimo do mesmo tipo real.
_TYPE_FAMILIES = {
    "string": {
        "string", "str", "varchar", "char", "text", "nvarchar", "nchar",
        "clob", "citext", "enum",
    },
    "integer": {
        "integer", "int", "bigint", "smallint", "tinyint", "serial",
        "bigserial", "int2", "int4", "int8", "long",
    },
    "decimal": {
        "decimal", "numeric", "number", "float", "double", "real", "money",
        "currency",
    },
    "date": {"date"},
    "datetime": {
        "datetime", "timestamp", "timestamptz", "smalldatetime", "datetime2",
    },
    "time": {"time", "timetz"},
    "boolean": {"boolean", "bool", "bit"},
    "binary": {"binary", "varbinary", "blob", "bytea", "image", "raw"},
    "json": {"json", "jsonb"},
    "uuid": {"uuid", "uniqueidentifier"},
}
_TYPE_ALIASES = {
    alias: family for family, aliases in _TYPE_FAMILIES.items() for alias in aliases
}

# Vereditos de um campo de destino.
CORRECT = "correct"
CORRECTLY_UNMAPPED = "correctly_unmapped"
WRONG_SOURCE = "wrong_source"
MISSED = "missed"
SPURIOUS = "spurious"
UNKNOWN_FIELD = "unknown_field"

_VERDICT_LABELS = {
    WRONG_SOURCE: "origem errada",
    MISSED: "faltou mapear",
    SPURIOUS: "mapeou campo que devia ficar sem origem",
    UNKNOWN_FIELD: "campo fora do schema",
}


def normalize_name(value: object) -> str:
    """Compara identificador sem caixa, espaço extra ou aspas de citação."""
    text = " ".join(str(value or "").strip().split())
    return text.strip('[]"`').casefold()


def type_family(raw: str | None) -> str:
    """`"decimal(18,2)"` -> `"decimal"`; `"varchar"` -> `"string"`."""
    text = " ".join((raw or "").strip().lower().split())
    text = text.split("(", 1)[0].strip()
    return _TYPE_ALIASES.get(text, text)


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def _qualified(table: str, field: str) -> str:
    return f"{table}.{field}"


def _pair(source_table: str | None, source_field: str | None) -> tuple[str, str] | None:
    if not (source_table or "").strip() or not (source_field or "").strip():
        return None
    return normalize_name(source_table), normalize_name(source_field)


def _predicted_pair(entry: MappingEntry | None) -> tuple[str, str] | None:
    if entry is None:
        return None
    return _pair(entry.source_table, entry.source_field)


def _expected_pair(row: GoldenMapping) -> tuple[str, str] | None:
    return _pair(row.source_table, row.source_field)


def _index_entries(
    mapping: MappingDocument,
) -> tuple[dict[tuple[str, str], MappingEntry], list[str]]:
    """Indexa as entradas por campo de destino, preferindo a que tem origem.

    Um artefato antigo pode ter duas entradas para o mesmo campo; a avaliação
    não pode ser refém da ordem. A duplicata é registrada para aparecer no
    relatório, não silenciada.
    """
    index: dict[tuple[str, str], MappingEntry] = {}
    duplicates: list[str] = []
    for entry in mapping.entries:
        key = (normalize_name(entry.target_table), normalize_name(entry.target_field))
        if key in index:
            duplicates.append(_qualified(entry.target_table, entry.target_field))
            if _predicted_pair(index[key]) is None and _predicted_pair(entry) is not None:
                index[key] = entry
            continue
        index[key] = entry
    return index, duplicates


def prediction_map(mapping: MappingDocument) -> dict[tuple[str, str], tuple[str, str] | None]:
    """O que o documento propôs, por campo de destino (para consistência)."""
    index, _ = _index_entries(mapping)
    return {key: _predicted_pair(entry) for key, entry in index.items()}


@dataclass(frozen=True)
class MappingOutcome:
    target_table: str
    target_field: str
    expected: tuple[str, str] | None
    predicted: tuple[str, str] | None
    verdict: str
    transformation_expected: str | None = None
    transformation_predicted: str | None = None
    transformation_evaluated: bool = False

    @property
    def transformation_ok(self) -> bool | None:
        if not self.transformation_evaluated:
            return None
        return self.transformation_expected == self.transformation_predicted

    @property
    def target(self) -> str:
        return _qualified(self.target_table, self.target_field)


def _counts(outcomes: list[MappingOutcome]) -> dict:
    tp = sum(1 for item in outcomes if item.verdict == CORRECT)
    wrong = sum(1 for item in outcomes if item.verdict == WRONG_SOURCE)
    missed = sum(1 for item in outcomes if item.verdict == MISSED)
    spurious = sum(1 for item in outcomes if item.verdict == SPURIOUS)
    tn = sum(1 for item in outcomes if item.verdict == CORRECTLY_UNMAPPED)

    # "Origem errada" é falso positivo (propôs uma origem que não é a certa)
    # e falso negativo (deixou de acertar a origem correta) ao mesmo tempo.
    fp = wrong + spurious
    fn = missed + wrong
    total = len(outcomes)
    expected_mapped = tp + wrong + missed

    return {
        "total_fields": total,
        "expected_mapped": expected_mapped,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
        "f1": _ratio(2 * tp, 2 * tp + fp + fn),
        "exact_accuracy": _ratio(tp + tn, total),
        "unmapped": {
            "total": tn + spurious,
            "correct": tn,
            "accuracy": _ratio(tn, tn + spurious),
        },
    }


def _fmt_pair(pair: tuple[str, str] | None, original: tuple[str | None, str | None]) -> str | None:
    if pair is None:
        return None
    table, field = original
    return _qualified(table or pair[0], field or pair[1])


def _outcome_of(row: GoldenMapping, entry: MappingEntry | None) -> MappingOutcome:
    expected = _expected_pair(row)
    predicted = _predicted_pair(entry)

    if expected is None:
        verdict = CORRECTLY_UNMAPPED if predicted is None else SPURIOUS
    elif predicted is None:
        verdict = MISSED
    elif predicted == expected:
        verdict = CORRECT
    else:
        verdict = WRONG_SOURCE

    evaluated = "transformation" in row.model_fields_set
    predicted_transformation = None
    if entry is not None and entry.transformation is not None:
        predicted_transformation = entry.transformation.type.strip().lower()

    return MappingOutcome(
        target_table=row.target_table,
        target_field=row.target_field,
        expected=expected,
        predicted=predicted,
        verdict=verdict,
        transformation_expected=(row.transformation or "").strip().lower() or None,
        transformation_predicted=predicted_transformation,
        transformation_evaluated=evaluated,
    )


def evaluate_mapping(
    golden_mapping: list[GoldenMapping],
    mapping: MappingDocument,
) -> dict:
    """Precisão/recall/F1 do mapeamento proposto contra o golden set."""
    index, duplicates = _index_entries(mapping)
    outcomes: list[MappingOutcome] = []
    errors: list[dict] = []

    golden_keys: set[tuple[str, str]] = set()
    for row in golden_mapping:
        key = (normalize_name(row.target_table), normalize_name(row.target_field))
        golden_keys.add(key)
        entry = index.get(key)
        outcome = _outcome_of(row, entry)
        outcomes.append(outcome)
        if outcome.verdict in {WRONG_SOURCE, MISSED, SPURIOUS}:
            errors.append(
                {
                    "target": outcome.target,
                    "expected": _fmt_pair(outcome.expected, (row.source_table, row.source_field)),
                    "predicted": _fmt_pair(
                        outcome.predicted,
                        (
                            entry.source_table if entry else None,
                            entry.source_field if entry else None,
                        ),
                    ),
                    "kind": outcome.verdict,
                    "label": _VERDICT_LABELS[outcome.verdict],
                }
            )

    # Previsões para campos que o golden nem lista: se o golden é exaustivo
    # (load_case garante que é), isso é erro do agente, não do caso.
    unknown: list[dict] = []
    for key, entry in index.items():
        if key in golden_keys:
            continue
        pair = _predicted_pair(entry)
        if pair is None:
            continue
        unknown.append(
            {
                "target": _qualified(entry.target_table, entry.target_field),
                "expected": None,
                "predicted": _qualified(entry.source_table or "", entry.source_field or ""),
                "kind": UNKNOWN_FIELD,
                "label": _VERDICT_LABELS[UNKNOWN_FIELD],
            }
        )
    errors.extend(unknown)

    metrics = _counts(outcomes)
    if unknown:
        # Campo fora do golden/universe também é falso positivo de precisão.
        metrics["fp"] += len(unknown)
        metrics["precision"] = _ratio(metrics["tp"], metrics["tp"] + metrics["fp"])
        metrics["f1"] = _ratio(
            2 * metrics["tp"], 2 * metrics["tp"] + metrics["fp"] + metrics["fn"]
        )

    by_table: OrderedDict[str, list[MappingOutcome]] = OrderedDict()
    for outcome in outcomes:
        by_table.setdefault(outcome.target_table, []).append(outcome)

    rows = []
    for table, table_outcomes in by_table.items():
        table_counts = _counts(table_outcomes)
        rows.append(
            {
                "table": table,
                "total_fields": table_counts["total_fields"],
                "precision": table_counts["precision"],
                "recall": table_counts["recall"],
                "f1": table_counts["f1"],
                "exact_accuracy": table_counts["exact_accuracy"],
                "errors": sum(
                    1
                    for item in table_outcomes
                    if item.verdict not in {CORRECT, CORRECTLY_UNMAPPED}
                ),
            }
        )

    evaluated = [item for item in outcomes if item.transformation_evaluated]
    transformation_errors = [
        {
            "target": item.target,
            "expected": item.transformation_expected,
            "predicted": item.transformation_predicted,
        }
        for item in evaluated
        if not item.transformation_ok
    ]

    return {
        "metrics": metrics,
        "by_table": rows,
        "errors": errors,
        "duplicates": sorted(set(duplicates)),
        "unknown_predictions": unknown,
        "transformations": {
            "evaluated": len(evaluated),
            "correct": sum(1 for item in evaluated if item.transformation_ok),
            "accuracy": _ratio(
                sum(1 for item in evaluated if item.transformation_ok), len(evaluated)
            ),
            "errors": transformation_errors,
        },
    }


def _column_index(report: DiscoveryReport) -> dict[tuple[str, str], ColumnProfile]:
    return {
        (normalize_name(table.table), normalize_name(column.name)): column
        for table in report.tables
        for column in table.columns
    }


def evaluate_discovery(
    golden_columns: list[GoldenColumn],
    report: DiscoveryReport,
) -> dict:
    """Acurácia de tipo e precisão/recall da flag de PII no discovery."""
    index = _column_index(report)

    type_evaluated = 0
    type_correct = 0
    type_errors: list[dict] = []

    pii_tp = pii_fp = pii_fn = pii_tn = 0
    pii_false_negatives: list[dict] = []
    pii_false_positives: list[dict] = []
    missing: list[str] = []
    undetermined_pii = 0

    for row in golden_columns:
        qualified = _qualified(row.table, row.column)
        column = index.get((normalize_name(row.table), normalize_name(row.column)))
        if column is None:
            missing.append(qualified)
            if row.inferred_type is not None:
                type_evaluated += 1
                type_errors.append(
                    {
                        "column": qualified,
                        "expected": row.inferred_type,
                        "predicted": None,
                        "kind": "ausente no relatório",
                    }
                )
            if row.pii is True:
                pii_fn += 1
                pii_false_negatives.append(
                    {
                        "column": qualified,
                        "predicted": None,
                        "note": "coluna ausente no relatório de discovery",
                    }
                )
            elif row.pii is False:
                undetermined_pii += 1
            continue

        if row.inferred_type is not None:
            type_evaluated += 1
            expected_family = type_family(row.inferred_type)
            predicted_family = type_family(column.inferred_type)
            if expected_family == predicted_family:
                type_correct += 1
            else:
                type_errors.append(
                    {
                        "column": qualified,
                        "expected": row.inferred_type,
                        "predicted": column.inferred_type,
                        "kind": f"{expected_family} != {predicted_family}",
                    }
                )

        if row.pii is True:
            if column.pii:
                pii_tp += 1
            else:
                pii_fn += 1
                pii_false_negatives.append(
                    {"column": qualified, "predicted": False, "note": "marcado como não-PII"}
                )
        elif row.pii is False:
            if column.pii:
                pii_fp += 1
                pii_false_positives.append(
                    {"column": qualified, "predicted": True, "note": "marcado como PII"}
                )
            else:
                pii_tn += 1

    return {
        "columns_expected": len(golden_columns),
        "columns_found": len(golden_columns) - len(missing),
        "missing": sorted(missing),
        "type": {
            "evaluated": type_evaluated,
            "correct": type_correct,
            "accuracy": _ratio(type_correct, type_evaluated),
            "errors": type_errors,
        },
        "pii": {
            "tp": pii_tp,
            "fp": pii_fp,
            "fn": pii_fn,
            "tn": pii_tn,
            "precision": _ratio(pii_tp, pii_tp + pii_fp),
            "recall": _ratio(pii_tp, pii_tp + pii_fn),
            "f1": _ratio(2 * pii_tp, 2 * pii_tp + pii_fp + pii_fn),
            "false_negatives": pii_false_negatives,
            "false_positives": pii_false_positives,
            "undetermined": undetermined_pii,
        },
    }
