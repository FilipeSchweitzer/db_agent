from __future__ import annotations

import json

from app.agents.agent2_mapping import MappingAgent
from app.schemas.profiling import ColumnProfile, DiscoveryReport, TableProfile
from app.schemas.target_schema import TargetField, TargetSchema, TargetTable
from support import ScriptedClient


def _target_schema() -> TargetSchema:
    return TargetSchema(
        version="teste",
        tables={
            "pessoa": TargetTable(
                fields={
                    "id": TargetField(type="integer", primary_key=True, required=True),
                    "cpf": TargetField(type="string", pii=True, required=True),
                    # Não será devolvido pelo modelo: deve cair em unmapped.
                    "nome": TargetField(type="string", required=True),
                }
            ),
            "bairro": TargetTable(
                fields={"id": TargetField(type="integer", primary_key=True, required=True)}
            ),
        },
    )


def _discovery() -> DiscoveryReport:
    return DiscoveryReport(
        source_dialect="firebird",
        tables=[
            TableProfile(
                table="legado_pessoa",
                row_count=10,
                columns=[
                    ColumnProfile(
                        name="CPF_LEG",
                        inferred_type="string",
                        null_rate=0.0,
                        pii=True,
                        notes="cpf do titular",
                    )
                ],
                risks=["sem FK declarada para bairro"],
            )
        ],
    )


def _entrada(field: str, **extra: object) -> dict[str, object]:
    return {
        "target_table": "pessoa",
        "target_field": field,
        "source_table": "legado_pessoa",
        "source_field": "CPF_LEG",
        "confidence": "high",
        "requires_human_review": False,
        "status": "pending_review",
        **extra,
    }


PESSOA = json.dumps(
    {
        "entries": [
            _entrada("cpf"),
            _entrada("id"),
            _entrada("campo_inventado"),
        ],
        "unmapped_target_fields": [],
    }
)
BAIRRO = json.dumps({"entries": [], "unmapped_target_fields": ["bairro.id"]})


def test_uma_chamada_por_tabela_de_destino():
    client = ScriptedClient([PESSOA, BAIRRO])

    mapping = MappingAgent(client).run("demo", _discovery(), _target_schema())

    assert len(client.calls) == 2
    assert mapping.project == "demo"


def test_tabela_de_destino_vem_do_schema_e_nao_do_modelo():
    client = ScriptedClient([PESSOA, BAIRRO])

    mapping = MappingAgent(client).run("demo", _discovery(), _target_schema())

    assert {entry.target_table for entry in mapping.entries} == {"pessoa"}


def test_entradas_seguem_a_ordem_do_schema():
    client = ScriptedClient([PESSOA, BAIRRO])

    mapping = MappingAgent(client).run("demo", _discovery(), _target_schema())

    assert [entry.target_field for entry in mapping.entries] == ["id", "cpf"]


def test_campo_inventado_e_descartado_com_aviso():
    client = ScriptedClient([PESSOA, BAIRRO])

    mapping = MappingAgent(client).run("demo", _discovery(), _target_schema())

    assert all(entry.target_field != "campo_inventado" for entry in mapping.entries)
    assert any("campo_inventado" in warning for warning in mapping.warnings)


def test_campo_sem_resposta_entra_em_unmapped():
    client = ScriptedClient([PESSOA, BAIRRO])

    mapping = MappingAgent(client).run("demo", _discovery(), _target_schema())

    assert "pessoa.nome" in mapping.unmapped_target_fields
    assert "bairro.id" in mapping.unmapped_target_fields
    assert any("sem entrada" in warning for warning in mapping.warnings)


def test_prompt_leva_indice_compacto_da_origem():
    client = ScriptedClient([PESSOA, BAIRRO])

    MappingAgent(client).run("demo", _discovery(), _target_schema())

    assert "legado_pessoa.CPF_LEG" in client.prompts[0]
    assert "sem FK declarada para bairro" in client.prompts[0]
    # O JSON completo do discovery não é despejado no prompt.
    assert "distinct_count" not in client.prompts[0]


def test_progresso_e_reportado_por_tabela():
    client = ScriptedClient([PESSOA, BAIRRO])
    vistos: list[tuple[int, int, str]] = []

    MappingAgent(client).run(
        "demo",
        _discovery(),
        _target_schema(),
        on_table=lambda pos, total, table: vistos.append((pos, total, table)),
    )

    assert vistos == [(1, 2, "pessoa"), (2, 2, "bairro")]
