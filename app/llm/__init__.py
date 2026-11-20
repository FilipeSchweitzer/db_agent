"""Seleção do backend de LLM por variável de ambiente.

`LLM_BACKEND` aceita:

  openai     (padrão) qualquer endpoint compatível com a API OpenAI —
             OpenAI oficial, Ollama, vLLM, LM Studio, OpenRouter...
             Configure com `LLM_BASE_URL`, `LLM_API_KEY` e `LLM_MODEL`.
  ollama     atalho para o Ollama local (`local` continua aceito como alias).
  anthropic  opcional: exige `pip install "migra-agent[anthropic]"`.

Não existe backend privilegiado: a escolha é só configuração, e os agentes
nunca sabem qual provedor está por trás.
"""
from __future__ import annotations

import os

from app.llm.client import LLMClient

BACKENDS = ("openai", "ollama", "anthropic")

# Aliases aceitos por compatibilidade com a configuração antiga.
_ALIASES = {"local": "ollama", "openai-compatible": "openai", "compatible": "openai"}


def normalize_backend_name(raw: str | None) -> str:
    """`local` -> `ollama`, `openai-compatible` -> `openai`, sem caixa."""
    value = (raw or "").strip().lower()
    return _ALIASES.get(value, value)


def current_backend_name() -> str:
    return normalize_backend_name(os.environ.get("LLM_BACKEND")) or "openai"


def _float_env(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} precisa ser numérico, recebido {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{name} não pode ser negativo: {value}")
    return value


def _retry_options() -> dict[str, float | int]:
    """Parâmetros de retry/backoff comuns a todos os backends.

    Ficam aqui, e não em cada backend, para que `LLM_MAX_ATTEMPTS` e
    `LLM_BACKOFF_BASE` valham igual em OpenAI, Ollama e Anthropic.
    """
    return {
        "max_attempts": int(_float_env("LLM_MAX_ATTEMPTS", 3)),
        "backoff_base": _float_env("LLM_BACKOFF_BASE", 1.0),
        "backoff_max": _float_env("LLM_BACKOFF_MAX", 8.0),
    }


def get_llm_client() -> LLMClient:
    backend = current_backend_name()
    retry = _retry_options()

    if backend == "openai":
        from app.llm.backend_openai import OpenAICompatibleBackend

        return OpenAICompatibleBackend(**retry)

    if backend == "ollama":
        from app.llm.backend_openai import OpenAICompatibleBackend

        return OpenAICompatibleBackend(
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434/v1"),
            api_key=os.environ.get("OLLAMA_API_KEY", "ollama"),
            model=os.environ.get("OLLAMA_MODEL", "qwen2.5:14b-instruct-q4_K_M"),
            **retry,
        )

    if backend == "anthropic":
        from app.llm.backend_anthropic import AnthropicBackend

        return AnthropicBackend(**retry)

    raise ValueError(
        f"LLM_BACKEND inválido: {backend!r} — use um de {', '.join(BACKENDS)}"
    )
