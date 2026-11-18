"""Checkpoint humano: exporta o mapeamento para .xlsx e lê de volta a
versão revisada pelo analista.

É deliberadamente uma planilha, não uma tela custom — reduz o atrito de
entrega nas primeiras versões do produto. Pode virar uma UI web depois
sem mudar o formato do MappingDocument.

O retorno deste módulo é a fronteira de confiança do pipeline: o que o
analista digitou vira `MappingEntry` validado. Nada é descartado em
silêncio — linha desconhecida, status inválido e célula vazia geram aviso
ou erro com o número da linha.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from app.schemas.mapping import MappingDocument, MappingEntry, ReviewStatus, Transformation

STATUS_VALUES = tuple(status.value for status in ReviewStatus)


def export_to_xlsx(mapping: MappingDocument, path: Path) -> None:
    rows = [
        {
            "target_table": e.target_table,
            "target_field": e.target_field,
            "source_table": e.source_table,
            "source_field": e.source_field,
            "transformation_type": e.transformation.type if e.transformation else "",
            # JSON, não repr de dict: é o que a leitura consegue reconstruir.
            "transformation_params": (
                json.dumps(e.transformation.params, ensure_ascii=False)
                if e.transformation
                else ""
            ),
            "confidence": e.confidence.value,
            "requires_human_review": e.requires_human_review,
            "review_reason": e.review_reason,
            "status": e.status.value,  # analista edita esta coluna: approved/edited/rejected
        }
        for e in mapping.entries
    ]
    df = pd.DataFrame(rows)
    df.to_excel(path, index=False, sheet_name="mapeamento")


def load_reviewed_xlsx(mapping: MappingDocument, path: Path) -> MappingDocument:
    """Relê a planilha após revisão humana e aplica os status/edições de volta
    ao MappingDocument — é este objeto que segue para o Agente 3."""
    df = pd.read_excel(path, sheet_name="mapeamento")
    entries_by_key = {(e.target_table, e.target_field): e for e in mapping.entries}

    reviewed: list[MappingEntry] = []
    warnings: list[str] = list(mapping.warnings)
    seen: set[tuple[str, str]] = set()

    for position, row in df.iterrows():
        line = int(position) + 2  # linha 1 é o cabeçalho da planilha
        key = (_clean(row.get("target_table")), _clean(row.get("target_field")))
        original = entries_by_key.get(key)  # type: ignore[arg-type]
        if original is None:
            warnings.append(
                f"planilha linha {line}: {key[0]}.{key[1]} não existe no mapeamento — ignorada."
            )
            continue
        if key in seen:
            warnings.append(f"planilha linha {line}: {key[0]}.{key[1]} duplicada — ignorada.")
            continue
        seen.add(key)

        status = _parse_status(row.get("status"), line)
        reviewed.append(
            MappingEntry(
                target_table=key[0],  # type: ignore[arg-type]
                target_field=key[1],  # type: ignore[arg-type]
                source_table=_clean(row.get("source_table")),
                source_field=_clean(row.get("source_field")),
                transformation=_parse_transformation(row, line),
                confidence=original.confidence,
                requires_human_review=original.requires_human_review,
                review_reason=_clean(row.get("review_reason")) or "",
                status=status,
            )
        )

    missing = [key for key in entries_by_key if key not in seen]
    for table, field in missing:
        original = entries_by_key[(table, field)]
        # Não some com a linha: ela continua no mapeamento como pendente, para
        # aparecer no relatório e no próximo round-trip.
        reviewed.append(
            original.model_copy(update={"status": ReviewStatus.pending_review})
        )
        warnings.append(
            f"{table}.{field}: não está na planilha devolvida — mantido como "
            "pendente de revisão."
        )

    mapping.entries = reviewed
    mapping.warnings = warnings
    return mapping


# ----------------------------------------------------------------------
# Internos
# ----------------------------------------------------------------------
def _clean(value) -> str | None:
    """Célula vazia do Excel vira NaN — normalizar para None."""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return str(value)


def _parse_status(value, line: int) -> ReviewStatus:
    text = _clean(value)
    if text is None:
        raise ValueError(
            f"planilha linha {line}: coluna 'status' vazia — use um de {STATUS_VALUES}."
        )
    try:
        return ReviewStatus(text.lower())
    except ValueError as exc:
        raise ValueError(
            f"planilha linha {line}: status {text!r} inválido — use um de {STATUS_VALUES}."
        ) from exc


def _parse_transformation(row, line: int) -> Transformation | None:
    kind = _clean(row.get("transformation_type"))
    if kind is None:
        return None

    raw_params = _clean(row.get("transformation_params"))
    if raw_params is None:
        return Transformation(type=kind)

    try:
        params = json.loads(raw_params)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"planilha linha {line}: transformation_params de {kind!r} não é JSON válido: "
            f"{raw_params!r}"
        ) from exc
    if not isinstance(params, dict):
        raise ValueError(
            f"planilha linha {line}: transformation_params precisa ser um objeto JSON, "
            f"recebido {type(params).__name__}."
        )
    return Transformation(type=kind, params=params)
