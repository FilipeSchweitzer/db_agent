"""Backend Anthropic — opcional, não é o caminho padrão.

A saída estruturada é obtida com **tool-use forçado**: o schema do Pydantic
vira o `input_schema` de uma tool e `tool_choice` obriga o modelo a chamá-la.
Assim a resposta já chega como objeto, sem JSON solto para parsear — o retry
da camada de base continua valendo para erros de validação semântica.

O SDK `anthropic` não é dependência do projeto: instale o extra
`pip install "migra-agent[anthropic]"` se quiser usar este backend.
"""
from __future__ import annotations

import json
import os
from typing import Any

from app.llm.client import LLMCallError, LLMClient, LLMError, LLMUsage, RawCompletion

TOOL_NAME = "emitir_resposta_estruturada"


class AnthropicBackend(LLMClient):
    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        max_tokens: int | None = None,
        max_attempts: int = 3,
        backoff_base: float = 1.0,
        backoff_max: float = 8.0,
    ) -> None:
        super().__init__(
            max_attempts=max_attempts,
            backoff_base=backoff_base,
            backoff_max=backoff_max,
        )

        try:
            import anthropic
        except ModuleNotFoundError as exc:  # pragma: no cover — depende do ambiente
            raise LLMError(
                'Backend Anthropic é opcional: instale com pip install "migra-agent[anthropic]" '
                "ou use LLM_BACKEND=openai / LLM_BACKEND=ollama."
            ) from exc

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError(
                "ANTHROPIC_API_KEY não definida. Use LLM_BACKEND=openai ou LLM_BACKEND=ollama "
                "se não quiser depender da Anthropic."
            )

        self.model = model or os.environ.get("LLM_MODEL") or "claude-sonnet-4-6"
        self.max_tokens = int(max_tokens or os.environ.get("LLM_MAX_TOKENS", 4096))
        self._client = anthropic.Anthropic(api_key=key)

    @property
    def model_name(self) -> str:
        return self.model

    def _invoke(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> RawCompletion:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system_prompt,
            tools=[
                {
                    "name": TOOL_NAME,
                    "description": "Emite a resposta estruturada pedida pelo usuário.",
                    "input_schema": schema,
                }
            ],
            tool_choice={"type": "tool", "name": TOOL_NAME},
            messages=[{"role": "user", "content": user_prompt}],
        )

        block = next(
            (item for item in response.content if getattr(item, "type", None) == "tool_use"),
            None,
        )
        if block is None:
            raise LLMCallError("O modelo não retornou o bloco de tool_use esperado.")

        usage = getattr(response, "usage", None)
        return RawCompletion(
            raw=json.dumps(block.input, ensure_ascii=False),
            usage=LLMUsage(
                prompt_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                completion_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            ),
        )
