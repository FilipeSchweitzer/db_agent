"""Plano de migração: ordem, de-para e o que entra no SQL.

O plano é a única camada que decide; o gerador só escreve. Se o plano está
errado, o SQL está errado de um jeito difícil de ver — por isso ele é
testado sem olhar uma linha de SQL.
"""
from __future__ import annotations

import pytest

from app.config import ETLConfig
from app.etl.plan import OWN_MAP_ALIAS, PlanError, build_plan
from app.schemas.mapping import ReviewStatus, Transformation
from tests.conftest import make_discovery, make_entry, make_mapping


def _plan_mapping(entries):
    from app.schemas.mapping import MappingDocument

    return MappingDocument(mapping_id="m", project="p", entries=entries)


def _plan(mapping, schema, **kwargs):
    options = {
        "mapping": mapping,
        "target_schema": schema,
        "source_dialect": "postgres",
        "target_dialect": "postgres",
        "source_schema": "legado",
        "target_schema_name": "public",
        "discovery": make_discovery(),
    }
    options.update(kwargs)
    return build_plan(**options)


def test_ordem_segue_dependencia_e_nao_o_yaml(schema, mapping):
    plan = _plan(mapping, schema)
    assert [step.table for step in plan.steps] == ["municipio", "bairro", "pessoa"]


def test_tabela_com_pendencia_nao_entra_no_plano(schema, mapping):
    plan = _plan(mapping, schema)
    assert "telefone" not in {step.table for step in plan.steps}
    assert all(
        column.target_field != "numero"
        for step in plan.steps
        for column in step.columns
    )


def test_entrada_edited_tambem_entra(schema):
    mapping = make_mapping()
    for entry in mapping.entries:
        if (entry.target_table, entry.target_field) == ("municipio", "uf"):
            entry.status = ReviewStatus.edited
    plan = _plan(mapping, schema)
    municipio = next(step for step in plan.steps if step.table == "municipio")
    assert "uf" in {column.target_field for column in municipio.columns}


def test_sem_nada_aprovado_falha_alto(schema, mapping):
    for entry in mapping.entries:
        entry.status = ReviewStatus.pending_review
    with pytest.raises(PlanError, match="Nenhuma entrada aprovada"):
        _plan(mapping, schema)


def test_tabela_com_duas_origens_e_ignorada_com_motivo(schema, mapping):
    mapping.entries.append(
        make_entry("municipio", "uf", "OUTRA_TABELA", "UF")
    )
    # Sem a FK de bairro -> municipio resolvida o plano falharia antes de
    # chegar ao motivo do descarte; aqui o foco é o descarte em si.
    plan = _plan(mapping, schema, etl=ETLConfig(allow_unresolved_fks=True))
    # A entrada extra não pode gerar JOIN implícito silencioso: a tabela é
    # descartada e o motivo aparece no plano.
    assert "municipio" in {item["table"] for item in plan.skipped}
    assert any("múltiplas tabelas" in item["reason"] for item in plan.skipped)


def test_fk_para_tabela_nao_migrada_falha_alto(schema):
    mapping = make_mapping()
    mapping.entries = [
        entry for entry in mapping.entries if entry.target_table == "pessoa"
    ]
    with pytest.raises(PlanError, match="cod_bairro"):
        _plan(mapping, schema)


def test_fk_nao_resolvida_pode_ser_aceita_explicitamente(schema):
    mapping = make_mapping()
    mapping.entries = [
        entry for entry in mapping.entries if entry.target_table == "pessoa"
    ]
    plan = _plan(mapping, schema, etl=ETLConfig(allow_unresolved_fks=True))
    pessoa = next(step for step in plan.steps if step.table == "pessoa")
    assert pessoa.foreign_keys == []
    assert any("cod_bairro" in warning for warning in pessoa.warnings)


def test_pk_gerada_cria_tabela_de_para_e_pk_vem_dela(schema, mapping):
    plan = _plan(mapping, schema)
    bairro = next(step for step in plan.steps if step.table == "bairro")
    assert bairro.sequence == "seq_bairro"
    assert bairro.map_table == "_map_bairro"
    assert bairro.old_key_expr == 's."COD_BAIRRO"'
    pk = bairro.pk_column
    assert pk is not None
    assert pk.source_expr == f'{OWN_MAP_ALIAS}."new_id"'
    assert pk.via_map is True


def test_fk_e_traduzida_pelo_de_para_da_tabela_pai(schema, mapping):
    plan = _plan(mapping, schema)
    pessoa = next(step for step in plan.steps if step.table == "pessoa")
    assert len(pessoa.foreign_keys) == 1
    fk = pessoa.foreign_keys[0]
    assert (fk.column, fk.parent_table, fk.parent_map) == (
        "cod_bairro",
        "bairro",
        "_map_bairro",
    )
    column = next(c for c in pessoa.columns if c.target_field == "cod_bairro")
    assert column.source_expr == 'm_cod_bairro."new_id"'
    assert column.via_map is True


def test_duas_fks_para_o_mesmo_pai_usam_aliases_distintos(schema):
    schema.tables["pessoa"].fields["cod_bairro_nascimento"] = type(
        schema.tables["pessoa"].fields["cod_bairro"]
    )(type="integer", references="bairro.id")
    mapping = make_mapping()
    mapping.entries.append(
        make_entry("pessoa", "cod_bairro_nascimento", "CAD_PESSOA", "COD_BAIRRO_NASC")
    )
    plan = _plan(mapping, schema)
    pessoa = next(step for step in plan.steps if step.table == "pessoa")
    aliases = {fk.alias for fk in pessoa.foreign_keys}
    assert aliases == {"m_cod_bairro", "m_cod_bairro_nascimento"}


def test_estrategia_preserve_nao_cria_de_para(schema, mapping):
    plan = _plan(mapping, schema, etl=ETLConfig(pk_strategy={"bairro": "preserve"}))
    bairro = next(step for step in plan.steps if step.table == "bairro")
    assert bairro.sequence is None
    assert bairro.map_table is None
    pessoa = next(step for step in plan.steps if step.table == "pessoa")
    assert pessoa.foreign_keys == []
    assert any("sem tradução" in warning for warning in pessoa.warnings)


def test_transformacao_e_renderizada_para_o_dialeto_de_origem(schema, mapping):
    postgres = _plan(mapping, schema, source_dialect="postgres")
    pessoa = next(step for step in postgres.steps if step.table == "pessoa")
    cpf = next(c for c in pessoa.columns if c.target_field == "cpf")
    assert cpf.source_expr == "regexp_replace(s.\"CPF\", '[^0-9]', '', 'g')"

    firebird = _plan(mapping, schema, source_dialect="firebird")
    pessoa = next(step for step in firebird.steps if step.table == "pessoa")
    cpf = next(c for c in pessoa.columns if c.target_field == "cpf")
    assert "REPLACE" in cpf.source_expr
    assert "REGEXP_REPLACE" not in cpf.source_expr


def test_map_enum_chega_ao_plano_como_case(schema):
    schema.tables["pessoa"].fields["sexo"] = type(
        schema.tables["pessoa"].fields["cod_bairro"]
    )(type="string")
    mapping = make_mapping()
    mapping.entries.append(
        make_entry(
            "pessoa",
            "sexo",
            "CAD_PESSOA",
            "SEXO",
            transformation=Transformation(
                type="map_enum", params={"mapping": {"M": "masculino"}}
            ),
        )
    )
    plan = _plan(mapping, schema)
    pessoa = next(step for step in plan.steps if step.table == "pessoa")
    sexo = next(c for c in pessoa.columns if c.target_field == "sexo")
    assert sexo.source_expr == (
        """CASE s."SEXO" WHEN 'M' THEN 'masculino' ELSE s."SEXO" END"""
    )


def test_estimativa_de_linhas_vem_do_discovery(schema, mapping):
    plan = _plan(mapping, schema)
    by_table = {step.table: step.estimated_rows for step in plan.steps}
    assert by_table == {"municipio": 10, "bairro": 40, "pessoa": 1000}


def test_volume_alto_gera_aviso(schema, mapping):
    plan = _plan(mapping, schema, etl=ETLConfig(high_volume_rows=100))
    assert any("1000" in warning for warning in plan.warnings)


def test_artefato_serializa_fk_e_de_para(schema, mapping):
    artifact = _plan(mapping, schema).to_artifact()
    pessoa = next(item for item in artifact["tables"] if item["table"] == "pessoa")
    assert pessoa["staging_table"] == "_map_pessoa"
    assert pessoa["foreign_keys"] == [
        {"column": "cod_bairro", "references": "bairro.id", "translated": True}
    ]
    assert artifact["entry_count"] == sum(
        len(item["fields"]) for item in artifact["tables"]
    )


def test_source_schema_ausente_nao_qualifica(schema, mapping):
    assert _plan(mapping, schema, source_schema=None).source_schema is None


# ----------------------------------------------------------------------
# PK sem origem aprovada
# ----------------------------------------------------------------------
def _apenas_numero(schema):
    """Aprova só o campo de dado de `telefone`, sem a PK."""
    schema.tables["telefone"].fields["numero"].required = True
    mapping = make_mapping()
    mapping.entries = [
        entry for entry in mapping.entries if entry.target_table == "telefone"
    ]
    mapping.entries[0].status = ReviewStatus.approved
    return mapping


def test_pk_sem_origem_nao_gera_de_para_e_o_banco_decide(schema):
    plan = _plan(_apenas_numero(schema), schema)
    assert any("sem origem aprovada" in warning for warning in plan.warnings)
    telefone = next(step for step in plan.steps if step.table == "telefone")

    assert telefone.map_table is None
    # A sequence continua existindo para o DEFAULT da coluna; o INSERT omite
    # a PK e o banco gera o valor.
    assert telefone.sequence == "seq_telefone"
    assert telefone.old_key_expr is None
    assert "id" not in {column.target_field for column in telefone.columns}


def test_fk_para_tabela_sem_de_para_falha_alto():
    from app.schemas.target_schema import TargetField, TargetSchema, TargetTable

    schema = TargetSchema(
        version="t",
        tables={
            "pai": TargetTable(
                fields={
                    "id": TargetField(type="integer", primary_key=True),
                    "nome": TargetField(type="string", required=True),
                }
            ),
            "filho": TargetTable(
                fields={
                    "id": TargetField(type="integer", primary_key=True),
                    "cod_pai": TargetField(type="integer", references="pai.id"),
                }
            ),
        },
    )
    mapping = _plan_mapping(
        entries=[
            make_entry("pai", "nome", "CAD_PAI", "NOME"),
            make_entry("filho", "id", "CAD_FILHO", "COD_FILHO"),
            make_entry("filho", "cod_pai", "CAD_FILHO", "COD_PAI"),
        ]
    )
    with pytest.raises(PlanError, match="PK de 'pai'"):
        _plan(mapping, schema)
