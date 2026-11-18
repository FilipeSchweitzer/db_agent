from __future__ import annotations

import pytest

from app.llm import BACKENDS, current_backend_name, get_llm_client
from app.llm.client import LLMError


@pytest.fixture(autouse=True)
def _ambiente_limpo(monkeypatch):
    """Nenhum teste depende do .env de quem roda a suíte."""
    for nome in (
        "LLM_BACKEND",
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_MAX_ATTEMPTS",
        "LLM_BACKOFF_BASE",
        "LLM_BACKOFF_MAX",
        "OLLAMA_BASE_URL",
        "OLLAMA_MODEL",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(nome, raising=False)


def test_backend_padrao_eh_openai(monkeypatch):
    monkeypatch.delenv("LLM_BACKEND", raising=False)

    assert current_backend_name() == "openai"


def test_local_eh_alias_de_ollama(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "local")

    assert current_backend_name() == "ollama"


def test_backend_invalido_falha_com_a_lista_de_opcoes(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "gemini")

    with pytest.raises(ValueError) as excinfo:
        get_llm_client()

    mensagem = str(excinfo.value)
    assert "gemini" in mensagem
    for backend in BACKENDS:
        assert backend in mensagem


def test_openai_sem_credencial_explica_o_que_falta(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "openai")

    with pytest.raises(LLMError) as excinfo:
        get_llm_client()

    mensagem = str(excinfo.value)
    assert "LLM_API_KEY" in mensagem
    assert "LLM_BASE_URL" in mensagem


def test_ollama_nao_exige_credencial(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "ollama")

    client = get_llm_client()

    assert client.model_name == "qwen2.5:14b-instruct-q4_K_M"


def test_variaveis_de_retry_valem_para_o_backend(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "ollama")
    monkeypatch.setenv("LLM_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("LLM_BACKOFF_BASE", "0.25")
    monkeypatch.setenv("LLM_BACKOFF_MAX", "2")

    client = get_llm_client()

    assert client.max_attempts == 5
    assert client.backoff_base == 0.25
    assert client.backoff_max == 2.0


def test_retry_invalido_falha_cedo(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "ollama")
    monkeypatch.setenv("LLM_MAX_ATTEMPTS", "muitas")

    with pytest.raises(ValueError):
        get_llm_client()
