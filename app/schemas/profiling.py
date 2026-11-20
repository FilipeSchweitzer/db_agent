"""Formato de saída do Agente 1 — nunca texto livre, sempre este schema."""
from __future__ import annotations

from pydantic import BaseModel


class ColumnProfile(BaseModel):
    name: str
    inferred_type: str
    null_rate: float
    distinct_count: int | None = None
    top_values: list[str] = []
    format_issues: list[str] = []
    pii: bool = False
    notes: str = ""


class TableProfile(BaseModel):
    table: str
    row_count: int
    columns: list[ColumnProfile]
    risks: list[str] = []


class DiscoveryReport(BaseModel):
    source_dialect: str
    tables: list[TableProfile]
    # Colunas em que o perfilamento (nome ou forma do valor) viu indício de
    # PII, mesmo que o modelo não tenha marcado `pii=true`. Fica no artefato
    # para a política de backend e a auditoria não dependerem do julgamento
    # do LLM.
    pii_columns: list[str] = []
