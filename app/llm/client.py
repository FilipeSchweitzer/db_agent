"""Contrato único entre os agentes e o provedor de LLM.

Um backend concreto implementa apenas `_invoke()` — transporte puro, uma
chamada ao provedor. Tudo o que é comum a *todos* os provedores vive aqui:

- parse do JSON (inclusive quando o modelo embrulha a resposta em cerca de
  markdown ou prefixa uma explicação);
- validação contra o modelo Pydantic;
- retry com backoff exponencial, **realimentando o erro de validação no
  prompt** para o modelo ter chance de corrigir a própria resposta;
- telemetria por chamada (tokens, latência, tentativa, erro), que o
  orquestrador grava em `output/<projeto>/run_log.json`.

A separação existe para que nenhum agente dependa do SDK de um provedor
específico: trocar de backend não muda uma linha em `app/agents/`.
"""
from __future__ import annotations

import json
import random
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

# Trecho da resposta anterior ecoado no prompt de correção. Curto o bastante
# para não estourar contexto, longo o bastante para o modelo se localizar.
_MAX_ECHO_CHARS = 1200

# Códigos HTTP em que vale a pena tentar de novo. 4xx "de uso" (401, 403, 404,
# 422) não entram: repetir não muda nada e só queima cota.
_TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

_TRANSIENT_NAME_FRAGMENTS = (
    "ratelimit",
    "apiconnection",
    "apitimeout",
    "timeout",
    "internalserver",
    "serviceunavailable",
    "overloaded",
)


class LLMError(RuntimeError):
    """Base para falhas de LLM tratadas pelo pipeline."""


class LLMCallError(LLMError):
    """A chamada ao provedor falhou (rede, autenticação, cota) após as tentativas."""


class LLMParseError(LLMError):
    """O provedor respondeu, mas o conteúdo não passou no schema após as tentativas."""


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class RawCompletion:
    """Retorno cru de um backend: texto do modelo + consumo informado pelo provedor."""

    raw: str
    usage: LLMUsage = field(default_factory=LLMUsage)


@dataclass
class CallStats:
    """Uma tentativa de chamada, bem-sucedida ou não."""

    attempt: int
    model: str
    ok: bool
    latency_ms: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str | None = None


def is_transient_error(exc: BaseException) -> bool:
    """Heurística de falha temporária: vale repetir a chamada?"""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int) and status in _TRANSIENT_STATUS:
        return True
    name = type(exc).__name__.lower()
    return any(fragment in name for fragment in _TRANSIENT_NAME_FRAGMENTS)


def clean_json_text(raw: str) -> str:
    """Extrai o JSON de uma resposta que pode vir com cerca de markdown ou prosa."""
    text = (raw or "").strip()
    if "```" in text:
        for part in text.split("```"):
            body = part.split("\n", 1)[1] if "\n" in part else part
            if body.strip().startswith(("{", "[")):
                text = body.strip()
                break
    if text and not text.startswith(("{", "[")):
        starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
        if starts:
            text = text[min(starts) :]
    return text.strip()


def parse_json_text(raw: str) -> Any:
    """Parse tolerante: aceita cerca de markdown e texto depois do JSON."""
    text = clean_json_text(raw)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        data, _ = json.JSONDecoder().raw_decode(text)
        return data


def build_correction(exc: BaseException, raw: str) -> str:
    """Mensagem anexada ao próximo prompt explicando por que a resposta foi rejeitada."""
    snippet = (raw or "").strip()
    if len(snippet) > _MAX_ECHO_CHARS:
        snippet = snippet[:_MAX_ECHO_CHARS] + "…"
    return (
        "\n\n---\n"
        "Sua resposta anterior NÃO passou na validação:\n"
        f"{type(exc).__name__}: {exc}\n\n"
        "Resposta anterior (pode estar incompleta ou malformada):\n"
        f"{snippet}\n\n"
        "Devolva o JSON completo e corrigido, sem comentários e sem texto fora do JSON."
    )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


class LLMClient(ABC):
    """Chamada estruturada com retry, validação e telemetria."""

    def __init__(
        self,
        *,
        max_attempts: int = 3,
        backoff_base: float = 1.0,
        backoff_max: float = 8.0,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts precisa ser >= 1")
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.calls: list[CallStats] = []

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Identificador do modelo — só para telemetria."""

    @abstractmethod
    def _invoke(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> RawCompletion:
        """Uma chamada crua ao provedor.

        Sem retry, sem parse, sem validação — só transporte. Implementações
        devem usar o modo de saída estruturada nativa do provedor quando
        existir (tool-use forçado, `response_format`), mas nunca depender
        dele para a correção: o retry da camada de cima cobre o resto.
        """

    # ------------------------------------------------------------------
    # Orquestração das tentativas
    # ------------------------------------------------------------------
    def complete_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
    ) -> T:
        schema = response_model.model_json_schema()
        correction = ""
        last_error: BaseException | None = None

        for attempt in range(1, self.max_attempts + 1):
            started = time.perf_counter()

            try:
                completion = self._invoke(system_prompt, user_prompt + correction, schema)
            except Exception as exc:  # noqa: BLE001 — repassado como LLMCallError
                usage = getattr(exc, "usage", None) or LLMUsage()
                self._record(attempt, _elapsed_ms(started), usage, error=exc)
                if not is_transient_error(exc) or attempt == self.max_attempts:
                    raise LLMCallError(self._call_failure(attempt, exc)) from exc
                self._pause(attempt)
                continue

            latency = _elapsed_ms(started)
            try:
                parsed = response_model.model_validate(parse_json_text(completion.raw))
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                self._record(attempt, latency, completion.usage, error=exc)
                last_error = exc
                correction = build_correction(exc, completion.raw)
                if attempt == self.max_attempts:
                    raise LLMParseError(
                        f"Resposta inválida de {self.model_name} após "
                        f"{attempt} tentativa(s): {type(exc).__name__}: {exc}"
                    ) from exc
                self._pause(attempt)
                continue

            self._record(attempt, latency, completion.usage)
            return parsed

        raise LLMParseError(  # pragma: no cover — inalcançável
            f"Resposta inválida de {self.model_name}: {last_error}"
        ) from last_error

    # ------------------------------------------------------------------
    # Telemetria
    # ------------------------------------------------------------------
    @property
    def totals(self) -> dict[str, int | float]:
        ok_calls = [call for call in self.calls if call.ok]
        return {
            "attempts": len(self.calls),
            "failed_attempts": len(self.calls) - len(ok_calls),
            "prompt_tokens": sum(call.prompt_tokens for call in self.calls),
            "completion_tokens": sum(call.completion_tokens for call in self.calls),
            "latency_ms": round(sum(call.latency_ms for call in self.calls), 1),
        }

    def telemetry(self) -> dict[str, Any]:
        """Bloco gravado no `run_log.json` pelo orquestrador."""
        return {
            "backend": type(self).__name__,
            "model": self.model_name,
            "totals": self.totals,
            "calls": [asdict(call) for call in self.calls],
        }

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------
    def _record(
        self,
        attempt: int,
        latency_ms: float,
        usage: LLMUsage,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.calls.append(
            CallStats(
                attempt=attempt,
                model=self.model_name,
                ok=error is None,
                latency_ms=latency_ms,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                error=None if error is None else f"{type(error).__name__}: {error}",
            )
        )

    def _pause(self, attempt: int) -> None:
        delay = min(self.backoff_max, self.backoff_base * (2 ** (attempt - 1)))
        # Jitter para não sincronizar retries de chamadas concorrentes.
        time.sleep(delay * (0.5 + random.random() / 2))

    def _call_failure(self, attempt: int, exc: BaseException) -> str:
        return (
            f"Falha ao chamar o LLM ({self.model_name}) após {attempt} tentativa(s): "
            f"{type(exc).__name__}: {exc}"
        )
