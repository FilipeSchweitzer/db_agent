from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from app.llm.client import (
    LLMCallError,
    LLMParseError,
    build_correction,
    clean_json_text,
    parse_json_text,
)
from support import FalhaTransitoria, ScriptedClient


class Resposta(BaseModel):
    nome: str
    idade: int


VALIDO = json.dumps({"nome": "Ana", "idade": 30})


def test_sucesso_na_primeira_tentativa():
    client = ScriptedClient([VALIDO])

    resultado = client.complete_structured("sistema", "usuario", Resposta)

    assert resultado.nome == "Ana"
    assert len(client.calls) == 1
    assert client.calls[0].ok is True
    assert client.calls[0].attempt == 1


def test_retry_quando_a_resposta_nao_e_json():
    client = ScriptedClient(["não consegui responder", VALIDO])

    resultado = client.complete_structured("sistema", "usuario", Resposta)

    assert resultado.idade == 30
    assert [call.attempt for call in client.calls] == [1, 2]
    assert [call.ok for call in client.calls] == [False, True]


def test_retry_realimenta_o_erro_de_validacao_no_prompt():
    invalido = json.dumps({"nome": "Ana"})  # falta idade
    client = ScriptedClient([invalido, VALIDO])

    client.complete_structured("sistema", "usuario", Resposta)

    assert "NÃO passou na validação" in client.prompts[1]
    assert "idade" in client.prompts[1]
    # O prompt original continua intacto na primeira tentativa.
    assert "NÃO passou na validação" not in client.prompts[0]


def test_esgotar_tentativas_levanta_parse_error():
    client = ScriptedClient(["lixo", "lixo", "lixo"])

    with pytest.raises(LLMParseError):
        client.complete_structured("sistema", "usuario", Resposta)

    assert len(client.calls) == 3
    assert all(call.ok is False for call in client.calls)
    assert all(call.error for call in client.calls)


def test_erro_transitorio_eh_repetido():
    client = ScriptedClient([FalhaTransitoria("429"), VALIDO])

    resultado = client.complete_structured("sistema", "usuario", Resposta)

    assert resultado.nome == "Ana"
    assert [call.ok for call in client.calls] == [False, True]
    assert "FalhaTransitoria" in (client.calls[0].error or "")


def test_erro_nao_transitorio_aborta_na_primeira():
    client = ScriptedClient([ValueError("401 unauthorized")])

    with pytest.raises(LLMCallError):
        client.complete_structured("sistema", "usuario", Resposta)

    assert len(client.calls) == 1


def test_aceita_json_em_cerca_de_markdown():
    embrulhado = f"```json\n{VALIDO}\n```"
    client = ScriptedClient([embrulhado])

    resultado = client.complete_structured("sistema", "usuario", Resposta)

    assert resultado.idade == 30


def test_aceita_texto_antes_do_json():
    client = ScriptedClient([f"Claro, aqui está:\n{VALIDO}\nEspero ter ajudado."])

    resultado = client.complete_structured("sistema", "usuario", Resposta)

    assert resultado.nome == "Ana"


def test_schema_e_enviado_ao_backend():
    client = ScriptedClient([VALIDO])

    client.complete_structured("sistema", "usuario", Resposta)

    assert client.schemas[0]["title"] == "Resposta"
    assert set(client.schemas[0]["properties"]) == {"nome", "idade"}


def test_totais_e_telemetria():
    client = ScriptedClient([VALIDO])

    client.complete_structured("sistema", "usuario", Resposta)

    assert client.totals["attempts"] == 1
    assert client.totals["prompt_tokens"] == 10
    assert client.totals["completion_tokens"] == 5

    telemetria = client.telemetry()
    assert telemetria["backend"] == "ScriptedClient"
    assert telemetria["model"] == "modelo-de-teste"
    assert telemetria["calls"][0]["latency_ms"] >= 0


def test_clean_json_text_remove_cerca_e_prosa():
    assert clean_json_text('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert clean_json_text('segue: {"a": 1} fim') == '{"a": 1} fim'


def test_parse_json_text_tolera_texto_depois_do_json():
    assert parse_json_text('{"a": 1} e mais um comentário') == {"a": 1}


def test_build_correction_trunca_resposta_gigante():
    correcao = build_correction(ValueError("campo faltando"), "x" * 10_000)

    assert "campo faltando" in correcao
    assert "…" in correcao
    assert len(correcao) < 2000
