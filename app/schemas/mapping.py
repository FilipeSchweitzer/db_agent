"""Formato do mapeamento — agnóstico de projeto, sempre a mesma forma.

É isto que alimenta a planilha de revisão humana e, depois de aprovado,
o Agente 3. O enum `status` é o que dá o controle do checkpoint humano.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class ReviewStatus(StrEnum):
    pending_review = "pending_review"
    approved = "approved"
    edited = "edited"
    rejected = "rejected"


class Confidence(StrEnum):
    high = "high"
    medium = "medium"
    low = "low"


class Transformation(BaseModel):
    type: str                      # nome no TRANSFORM_REGISTRY — nunca código livre
    params: dict = {}


class MappingEntry(BaseModel):
    target_table: str
    target_field: str
    source_table: str | None = None
    source_field: str | None = None
    transformation: Transformation | None = None
    confidence: Confidence
    requires_human_review: bool
    review_reason: str = ""
    status: ReviewStatus = ReviewStatus.pending_review


class MappingDocument(BaseModel):
    mapping_id: str
    project: str
    entries: list[MappingEntry]
    unmapped_target_fields: list[str] = []
    unmapped_source_fields: list[str] = []
