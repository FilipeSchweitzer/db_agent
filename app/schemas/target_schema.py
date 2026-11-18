"""Modelos para o target_schema.yaml — o schema de destino como dado.

Além dos modelos, este módulo extrai do schema o que a geração de ETL precisa
saber: quais campos referenciam quais tabelas e em que ordem as tabelas podem
ser carregadas sem violar FK (ordem topológica).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel

# Tipos tratados como numéricos inteiros — candidatos a PK gerada por sequence.
INTEGER_TYPES = {"integer", "int", "bigint", "smallint", "serial", "bigserial"}


def base_type(type_spec: str | None) -> str:
    """`"decimal(18,2)"` -> `"decimal"`."""
    return (type_spec or "").split("(", 1)[0].strip().lower()


class TargetField(BaseModel):
    type: str
    required: bool = False
    primary_key: bool = False
    pii: bool = False
    format: str | None = None
    validator: str | None = None
    references: str | None = None  # ex: "bairro.id"

    @property
    def is_integer(self) -> bool:
        return base_type(self.type) in INTEGER_TYPES


class TargetTable(BaseModel):
    fields: dict[str, TargetField]

    @property
    def primary_key(self) -> str | None:
        for name, field in self.fields.items():
            if field.primary_key:
                return name
        return None


@dataclass(frozen=True)
class Reference:
    """FK declarada no schema: `tabela.campo -> ref_table.ref_field`."""

    table: str
    field: str
    ref_table: str
    ref_field: str


class TargetSchema(BaseModel):
    version: str
    tables: dict[str, TargetTable]

    @classmethod
    def load(cls, path: Path) -> TargetSchema:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(**data)

    # ------------------------------------------------------------------
    # Grafo de dependência
    # ------------------------------------------------------------------
    def references(self) -> list[Reference]:
        """Todas as FKs declaradas, validadas contra o próprio schema.

        Referência malformada ou apontando para tabela/campo inexistente é
        erro de configuração — falhar aqui evita gerar script que não roda.
        """
        found: list[Reference] = []
        for table_name, table in self.tables.items():
            for field_name, field in table.fields.items():
                raw = (field.references or "").strip()
                if not raw:
                    continue
                parts = raw.split(".")
                if len(parts) != 2 or not all(part.strip() for part in parts):
                    raise ValueError(
                        f"{table_name}.{field_name}: references={raw!r} — "
                        "esperado o formato 'tabela.campo'"
                    )
                ref_table, ref_field = (part.strip() for part in parts)
                if ref_table not in self.tables:
                    raise ValueError(
                        f"{table_name}.{field_name}: references aponta para a tabela "
                        f"'{ref_table}', que não existe no target schema"
                    )
                if ref_field not in self.tables[ref_table].fields:
                    raise ValueError(
                        f"{table_name}.{field_name}: references aponta para "
                        f"'{ref_table}.{ref_field}', que não existe no target schema"
                    )
                found.append(Reference(table_name, field_name, ref_table, ref_field))
        return found

    def pk_strategy_default(self, table: str) -> str:
        """PK inteira é gerada pelo destino; qualquer outra é copiada da origem."""
        pk = self.tables[table].primary_key
        if pk is None:
            return "preserve"
        return "sequence" if self.tables[table].fields[pk].is_integer else "preserve"

    def dependency_order(self, tables: Iterable[str] | None = None) -> list[str]:
        """Ordem de carga: toda tabela depois das tabelas que ela referencia.

        Determinística (segue a ordem do YAML entre nós independentes) e
        levanta `ValueError` com o ciclo quando não existe ordem possível.
        """
        selected = list(self.tables) if tables is None else [t for t in tables if t in self.tables]
        allowed = set(selected)
        parents: dict[str, list[str]] = {table: [] for table in selected}
        for ref in self.references():
            if ref.table in allowed and ref.ref_table in allowed and ref.ref_table != ref.table:
                parents[ref.table].append(ref.ref_table)

        remaining = list(selected)
        order: list[str] = []
        while remaining:
            ready = [t for t in remaining if all(p in order for p in parents[t])]
            if not ready:
                raise ValueError(
                    "Ciclo de referência no target schema, sem ordem de carga possível: "
                    + " -> ".join(_find_cycle(parents, remaining))
                )
            for table in ready:
                order.append(table)
                remaining.remove(table)
        return order

    def fk_targets(self, table: str) -> dict[str, Reference]:
        """Campos de `table` que são FK, indexados pelo nome do campo."""
        return {ref.field: ref for ref in self.references() if ref.table == table}


def _find_cycle(parents: dict[str, list[str]], nodes: list[str]) -> list[str]:
    """Caminho de um ciclo, só para a mensagem de erro ser acionável."""
    node = nodes[0]
    path = [node]
    seen = {node}
    while True:
        candidates = [p for p in parents.get(node, []) if p in nodes]
        if not candidates:
            return path
        node = candidates[0]
        if node in seen:
            path.append(node)
            return path
        path.append(node)
        seen.add(node)
