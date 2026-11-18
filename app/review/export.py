"""Checkpoint humano: exporta o mapeamento para .xlsx e lê de volta a
versão revisada pelo analista.

É deliberadamente uma planilha, não uma tela custom — reduz o atrito de
entrega nas primeiras versões do produto. Pode virar uma UI web depois
sem mudar o formato do MappingDocument.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.schemas.mapping import MappingDocument, MappingEntry, ReviewStatus, Transformation


def export_to_xlsx(mapping: MappingDocument, path: Path) -> None:
    rows = [
        {
            "target_table": e.target_table,
            "target_field": e.target_field,
            "source_table": e.source_table,
            "source_field": e.source_field,
            "transformation_type": e.transformation.type if e.transformation else "",
            "transformation_params": e.transformation.params if e.transformation else {},
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
    for _, row in df.iterrows():
        key = (row["target_table"], row["target_field"])
        original = entries_by_key.get(key)
        if original is None:
            continue

        transformation = None
        if row.get("transformation_type"):
            transformation = Transformation(
                type=row["transformation_type"],
                params=row.get("transformation_params") or {},
            )

        reviewed.append(
            MappingEntry(
                target_table=row["target_table"],
                target_field=row["target_field"],
                source_table=row.get("source_table") or None,
                source_field=row.get("source_field") or None,
                transformation=transformation,
                confidence=original.confidence,
                requires_human_review=original.requires_human_review,
                review_reason=row.get("review_reason", ""),
                status=ReviewStatus(row["status"]),
            )
        )

    mapping.entries = reviewed
    return mapping
