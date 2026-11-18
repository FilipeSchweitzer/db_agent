"""Dublês de teste — nenhum teste fala com provedor de LLM ou banco."""
from __future__ import annotations

from typing import Any

from app.llm.client import LLMClient, LLMUsage, RawCompletion


class ScriptedClient(LLMClient):
    """LLMClient com respostas roteirizadas.

    Cada item do script é o texto cru de uma tentativa, ou uma exceção a ser
    levantada. Assim dá para testar sucesso direto, retry e esgotamento de
    tentativas sem rede.
    """

    def __init__(
        self,
        script: list[str | BaseException],
        *,
        usage: LLMUsage | None = None,
    ) -> None:
        super().__init__(max_attempts=3, backoff_base=0.0, backoff_max=0.0)
        self._script = list(script)
        self._usage = usage or LLMUsage(prompt_tokens=10, completion_tokens=5)
        self.prompts: list[str] = []
        self.schemas: list[dict[str, Any]] = []

    @property
    def model_name(self) -> str:
        return "modelo-de-teste"

    def _invoke(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> RawCompletion:
        self.prompts.append(user_prompt)
        self.schemas.append(schema)
        if not self._script:
            raise AssertionError("script do ScriptedClient esgotado")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return RawCompletion(raw=item, usage=self._usage)


class FalhaTransitoria(Exception):
    """Simula 429/5xx: o retry deve tentar de novo."""

    status_code = 429
