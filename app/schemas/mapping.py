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


class TableMapping(BaseModel):
    """Resposta do Agente 2 para UMA tabela de destino.

    O mapeamento é pedido tabela a tabela porque um schema de destino real
    tem dezenas de campos: o `MappingDocument` completo não cabe numa única
    resposta de 4096 tokens, e tentar isso truncava o JSON no meio.
    """

    entries: list[MappingEntry]
    unmapped_target_fields: list[str] = []


class MappingDocument(BaseModel):
    mapping_id: str
    project: str
    entries: list[MappingEntry]
    # Versão do target_schema contra a qual o mapeamento foi proposto. O
    # `generate` recusa rodar se o schema em disco mudou desde então — mapear
    # um campo que deixou de existir produz script que não roda.
    target_schema_version: str | None = None
    unmapped_target_fields: list[str] = []
    unmapped_source_fields: list[str] = []
    # Ajustes que o pipeline fez por conta própria ao montar o documento
    # (campo inventado pelo modelo, entrada duplicada, campo sem resposta).
    # Fica no artefato para o analista ver o que não veio do LLM.
    warnings: list[str] = []
