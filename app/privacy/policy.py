"""Regra de negócio: dado pessoal não sai da rede.

Quando o perfilamento encontra indício de PII, o backend de LLM precisa ser
**comprovadamente local**. Não é preferência configurável nem aviso no log:
é uma exceção que interrompe o pipeline antes da primeira chamada.

Sem PII, nenhum backend é exigido — o invariante da Fase 1 (nenhum provedor
obrigatório) continua valendo.

O que conta como local:

- `LLM_BACKEND=ollama` (ou o alias antigo `local`);
- `LLM_BACKEND=openai` com `LLM_BASE_URL` apontando para loopback
  (`localhost`, `127.0.0.1`), IP privado (RFC 1918), nome de serviço sem
  ponto (`ollama`, típico de docker compose) ou sufixo interno
  (`.local`, `.lan`, `.internal`).

`anthropic` é sempre remoto: é a API oficial, não há endpoint local.
"""
from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlparse

from app.llm import current_backend_name, normalize_backend_name

# Nomes que só existem dentro da máquina/rede local.
_LOCAL_NAMES = {
    "localhost",
    "ollama",
    "host.docker.internal",
    "gateway.docker.internal",
}

# Domínios internos. Dado continua na rede da organização.
_LOCAL_SUFFIXES = (".local", ".lan", ".internal", ".home", ".localdomain", ".intranet")


class PIIPolicyError(RuntimeError):
    """Há PII no escopo e o backend de LLM configurado não é local."""


def _host_is_local(host: str | None) -> bool:
    host = (host or "").strip().lower().strip("[]")
    if not host:
        return False
    if host in _LOCAL_NAMES:
        return True

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return address.is_loopback or address.is_private or address.is_link_local

    if host.endswith(_LOCAL_SUFFIXES):
        return True
    # Nome de serviço (docker compose, /etc/hosts) não tem ponto.
    return "." not in host


def _base_url(backend: str, base_url: str | None) -> str | None:
    if base_url is not None:
        return base_url
    if backend == "ollama":
        # `ollama` é atalho para o serviço local; ainda assim, o host efetivo
        # vem de OLLAMA_BASE_URL e é ele que a política precisa conferir.
        return os.environ.get("OLLAMA_BASE_URL") or "http://ollama:11434/v1"
    if backend == "openai":
        return os.environ.get("LLM_BASE_URL") or None
    if backend == "anthropic":
        return os.environ.get("ANTHROPIC_BASE_URL") or None
    return None


def backend_is_local(
    backend: str | None = None,
    base_url: str | None = None,
) -> bool:
    """O backend configurado mantém o dado dentro da rede local?"""
    backend = normalize_backend_name(backend or current_backend_name())
    if backend not in {"ollama", "openai", "anthropic"}:
        return False

    url = _base_url(backend, base_url)
    if not url:
        return False
    return _host_is_local(urlparse(url).hostname)


def enforce_pii_backend_policy(
    has_pii: bool,
    *,
    backend: str | None = None,
    base_url: str | None = None,
) -> None:
    """Interrompe a execução quando PII encontraria um backend remoto.

    Chamado **depois** do perfilamento (que é local, só lê o banco) e
    **antes** da primeira chamada de LLM. Sem PII, não faz nada.
    """
    if not has_pii:
        return

    resolved = normalize_backend_name(backend or current_backend_name())
    url = _base_url(resolved, base_url)
    if backend_is_local(resolved, url):
        return

    where = url or "endpoint padrão do provedor"
    raise PIIPolicyError(
        "PII detectada no escopo, mas o backend de LLM é remoto "
        f"(LLM_BACKEND={resolved}, destino: {where}).\n"
        "Nenhum valor de dado pessoal sai da rede por configuração.\n"
        "Use um endpoint local:\n"
        "  LLM_BACKEND=local                 # Ollama local (alias de 'ollama')\n"
        "  LLM_BASE_URL=http://ollama:11434/v1\n"
        "ou aponte LLM_BASE_URL para um host da rede interna.\n"
        "Se não houver PII, o backend remoto continua permitido."
    )
