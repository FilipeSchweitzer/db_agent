"""Target schema como fonte da verdade de FK e de ordem de carga.

`references` existia no YAML desde a Fase 1 e não era lido por ninguém: o
script gerado copiava a chave antiga como se fosse a nova. Estes testes
travam o contrato que corrige isso.
"""
from __future__ import annotations

import pytest

from app.schemas.target_schema import TargetField, TargetSchema, TargetTable


def _schema(tables: dict[str, dict[str, TargetField]]) -> TargetSchema:
    return TargetSchema(
        version="t",
        tables={name: TargetTable(fields=fields) for name, fields in tables.items()},
    )


def test_references_extrai_tabela_e_campo():
    schema = _schema(
        {
            "pai": {"id": TargetField(type="integer", primary_key=True)},
            "filho": {"id": TargetField(type="integer", primary_key=True),
                      "cod_pai": TargetField(type="integer", references="pai.id")},
        }
    )
    refs = schema.references()
    assert [(r.table, r.field, r.ref_table, r.ref_field) for r in refs] == [
        ("filho", "cod_pai", "pai", "id")
    ]


@pytest.mark.parametrize("raw", ["pai", "pai.id.extra", "."])
def test_references_malformada_falha_alto(raw):
    schema = _schema(
        {
            "pai": {"id": TargetField(type="integer", primary_key=True)},
            "filho": {"cod_pai": TargetField(type="integer", references=raw)},
        }
    )
    with pytest.raises(ValueError, match="tabela.campo"):
        schema.references()


def test_references_para_tabela_inexistente_falha_alto():
    schema = _schema({"filho": {"cod_pai": TargetField(type="integer", references="pai.id")}})
    with pytest.raises(ValueError, match="não existe no target schema"):
        schema.references()


def test_references_para_campo_inexistente_falha_alto():
    schema = _schema(
        {
            "pai": {"id": TargetField(type="integer", primary_key=True)},
            "filho": {"cod_pai": TargetField(type="integer", references="pai.uuid")},
        }
    )
    with pytest.raises(ValueError, match="pai.uuid"):
        schema.references()


def test_dependency_order_coloca_o_pai_antes_do_filho(schema):
    assert schema.dependency_order(["pessoa", "bairro", "municipio"]) == [
        "municipio",
        "bairro",
        "pessoa",
    ]


def test_dependency_order_e_deterministica_entre_independentes():
    schema = _schema(
        {
            "a": {"id": TargetField(type="integer", primary_key=True)},
            "b": {"id": TargetField(type="integer", primary_key=True)},
            "c": {"id": TargetField(type="integer", primary_key=True)},
        }
    )
    assert schema.dependency_order(["a", "b", "c"]) == ["a", "b", "c"]


def test_dependency_order_ignora_referencia_fora_do_conjunto(schema):
    # Só `pessoa` é migrada: a FK para bairro não impede a carga.
    assert schema.dependency_order(["pessoa"]) == ["pessoa"]


def test_dependency_order_levanta_ciclo_com_o_caminho():
    schema = _schema(
        {
            "a": {"id": TargetField(type="integer", primary_key=True),
                  "b_id": TargetField(type="integer", references="b.id")},
            "b": {"id": TargetField(type="integer", primary_key=True),
                  "a_id": TargetField(type="integer", references="a.id")},
        }
    )
    with pytest.raises(ValueError, match="Ciclo"):
        schema.dependency_order()


def test_pk_strategy_default_e_sequence_para_inteiro(schema):
    assert schema.pk_strategy_default("bairro") == "sequence"


def test_pk_strategy_default_e_preserve_para_chave_texto():
    schema = _schema(
        {"t": {"codigo": TargetField(type="string", primary_key=True)}}
    )
    assert schema.pk_strategy_default("t") == "preserve"


def test_pk_strategy_default_e_preserve_sem_pk():
    schema = _schema({"t": {"nome": TargetField(type="string")}})
    assert schema.pk_strategy_default("t") == "preserve"


def test_fk_targets_indexa_pelo_campo_do_filho(schema):
    targets = schema.fk_targets("pessoa")
    assert list(targets) == ["cod_bairro"]
    assert targets["cod_bairro"].ref_table == "bairro"
