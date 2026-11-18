"""Fixtures compartilhadas: um schema de destino de 3 tabelas relacionadas.

O caso de aceitação da Fase 2 é exatamente este formato — `municipio` é
referenciada por `bairro`, que é referenciada por `pessoa`. Os testes de
plano e de geração usam o mesmo schema para que a ordem de carga e o de-para
sejam exercitados de ponta a ponta, e não em tabelas soltas.
"""
from __future__ import annotations

import uuid

import pytest

from app.schemas.mapping import (
    Confidence,
    MappingDocument,
    MappingEntry,
    ReviewStatus,
    Transformation,
)
from app.schemas.profiling import DiscoveryReport, TableProfile
from app.schemas.target_schema import TargetField, TargetSchema, TargetTable


def make_schema() -> TargetSchema:
    return TargetSchema(
        version="test.1",
        tables={
            # Deliberadamente fora de ordem: quem manda é a FK, não o YAML.
            "pessoa": TargetTable(
                fields={
                    "id": TargetField(type="integer", required=True, primary_key=True),
                    "cpf": TargetField(type="string", required=True, pii=True),
                    "nome_completo": TargetField(type="string", required=True),
                    "data_nascimento": TargetField(type="date"),
                    "cod_bairro": TargetField(type="integer", references="bairro.id"),
                }
            ),
            "bairro": TargetTable(
                fields={
                    "id": TargetField(type="integer", required=True, primary_key=True),
                    "nome": TargetField(type="string", required=True),
                    "cod_municipio": TargetField(type="integer", references="municipio.id"),
                }
            ),
            "municipio": TargetTable(
                fields={
                    "id": TargetField(type="integer", required=True, primary_key=True),
                    "nome": TargetField(type="string", required=True),
                    "uf": TargetField(type="string(2)"),
                }
            ),
            "telefone": TargetTable(
                fields={
                    "id": TargetField(type="integer", required=True, primary_key=True),
                    "numero": TargetField(type="string", required=True),
                    "cod_pessoa": TargetField(type="integer", references="pessoa.id"),
                }
            ),
        },
    )


def make_entry(
    table: str,
    field: str,
    source_table: str | None,
    source_field: str | None,
    *,
    transformation: Transformation | None = None,
    status: ReviewStatus = ReviewStatus.approved,
) -> MappingEntry:
    return MappingEntry(
        target_table=table,
        target_field=field,
        source_table=source_table,
        source_field=source_field,
        transformation=transformation,
        confidence=Confidence.high,
        requires_human_review=status is ReviewStatus.pending_review,
        status=status,
    )


def make_mapping() -> MappingDocument:
    entries = [
        make_entry("municipio", "id", "CAD_MUNICIPIO", "COD_MUN"),
        make_entry("municipio", "nome", "CAD_MUNICIPIO", "NOME_MUN"),
        make_entry("municipio", "uf", "CAD_MUNICIPIO", "UF"),
        make_entry("bairro", "id", "CAD_BAIRRO", "COD_BAIRRO"),
        make_entry("bairro", "nome", "CAD_BAIRRO", "NOME_BAIRRO"),
        make_entry("bairro", "cod_municipio", "CAD_BAIRRO", "COD_MUN"),
        make_entry("pessoa", "id", "CAD_PESSOA", "COD_PESSOA"),
        make_entry(
            "pessoa",
            "cpf",
            "CAD_PESSOA",
            "CPF",
            transformation=Transformation(type="regex_extract_digits"),
        ),
        make_entry("pessoa", "nome_completo", "CAD_PESSOA", "NOME"),
        make_entry(
            "pessoa",
            "data_nascimento",
            "CAD_PESSOA",
            "DT_NASC",
            transformation=Transformation(type="parse_date_br"),
        ),
        make_entry("pessoa", "cod_bairro", "CAD_PESSOA", "COD_BAIRRO"),
        # Fica de fora do SQL até alguém aprovar: é o checkpoint humano.
        make_entry(
            "telefone",
            "numero",
            "CAD_TELEFONE",
            "NUMERO",
            status=ReviewStatus.pending_review,
        ),
    ]
    return MappingDocument(
        mapping_id=str(uuid.uuid4()),
        project="test-client",
        entries=entries,
    )


def make_discovery() -> DiscoveryReport:
    return DiscoveryReport(
        source_dialect="postgres",
        tables=[
            TableProfile(table="CAD_MUNICIPIO", row_count=10, columns=[]),
            TableProfile(table="CAD_BAIRRO", row_count=40, columns=[]),
            TableProfile(table="CAD_PESSOA", row_count=1000, columns=[]),
        ],
    )


@pytest.fixture
def schema() -> TargetSchema:
    return make_schema()


@pytest.fixture
def mapping() -> MappingDocument:
    return make_mapping()


@pytest.fixture
def discovery() -> DiscoveryReport:
    return make_discovery()
