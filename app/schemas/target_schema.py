"""Modelos para o target_schema.yaml — o schema de destino como dado."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel


class TargetField(BaseModel):
    type: str
    required: bool = False
    primary_key: bool = False
    pii: bool = False
    format: str | None = None
    validator: str | None = None
    references: str | None = None  # ex: "bairro.id"


class TargetTable(BaseModel):
    fields: dict[str, TargetField]


class TargetSchema(BaseModel):
    version: str
    tables: dict[str, TargetTable]

    @classmethod
    def load(cls, path: Path) -> "TargetSchema":
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(**data)
