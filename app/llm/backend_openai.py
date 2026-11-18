"""Backend para qualquer endpoint compatível com a API OpenAI.

Um backend cobre OpenAI, Ollama, vLLM, LM Studio, OpenRouter e afins — a
diferença é só `base_url` e modelo. Não há dependência de nenhum provedor
específico no caminho padrão.

A saída estruturada usa `response_format`:
  1. primeiro `json_schema` com `strict=True` (garantia de schema no lado do
     provedor, quando ele suporta);
  2. se a API recusar o formato, cai para `json_object` uma única vez e
     memoriza a decisão — o schema continua sendo cobrado pelo prompt e pelo
     retry da camada de cima.
"""
from __future__ import annotations

import json
import os
from typing import Any

from openai import OpenAI

from app.llm.client import LLMCallError, LLMClient, LLMError, LLMUsage, RawCompletion

# Palavras-chave do JSON Schema que o modo estrito não aceita (ou que exigem
# que o campo seja obrigatório, o que conflita com `default`).
_STRICT_UNSUPPORTED_KEYS = {"$schema", "default", "examples", "title"}

_SCHEMA_REJECTION_FRAGMENTS = (
    "response_format",
    "json_schema",
    "additionalproperties",
    "unknown parameter",
    "unsupported",
    "invalid schema",
)


def to_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Adapta um JSON Schema do Pydantic ao subconjunto aceito por
    `response_format={"type": "json_schema", "strict": True}`.

    No modo estrito todo objeto precisa de `additionalProperties: false` e de
    todas as propriedades listadas em `required`. Campos com valor padrão no
    Pydantic não entram em `required`, então o `default` sai do schema e o
    campo passa a ser exigido — quem preenche é o modelo, não o validador.
    """
    return _walk(schema)


def _walk(node: Any) -> Any:
    if isinstance(node, list):
        return [_walk(item) for item in node]
    if not isinstance(node, dict):
        return node

    cleaned: dict[str, Any] = {}
    for key, value in node.items():
        if key in _STRICT_UNSUPPORTED_KEYS:
            continue
        # `properties` é um mapa nome-de-campo -> schema: as chaves aqui são
        # nomes de campo, não palavras-chave do JSON Schema, e um campo
        # chamado "title" não pode ser removido por engano.
        if key == "properties" and isinstance(value, dict):
            cleaned[key] = {name: _walk(spec) for name, spec in value.items()}
        else:
            cleaned[key] = _walk(value)

    properties = cleaned.get("properties")
    if isinstance(properties, dict):
        cleaned["additionalProperties"] = False
        cleaned["required"] = list(properties.keys())
    return cleaned


class OpenAICompatibleBackend(LLMClient):
    """`LLM_BASE_URL` vazio => OpenAI oficial. Preenchido => qualquer endpoint
    compatível (Ollama, vLLM, OpenRouter, ...)."""

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        max_attempts: int = 3,
        backoff_base: float = 1.0,
        backoff_max: float = 8.0,
    ) -> None:
        super().__init__(
            max_attempts=max_attempts,
            backoff_base=backoff_base,
            backoff_max=backoff_max,
        )
        self.model = model or os.environ.get("LLM_MODEL") or "gpt-4o-mini"
        self.max_tokens = int(max_tokens or os.environ.get("LLM_MAX_TOKENS", 4096))
        self.temperature = float(
            temperature if temperature is not None else os.environ.get("LLM_TEMPERATURE", 0.0)
        )
        self._base_url = base_url or os.environ.get("LLM_BASE_URL") or None
        self._api_key = api_key or os.environ.get("LLM_API_KEY") or None
        self._strict_supported = True
        self._client = self._build_client()

    @property
    def model_name(self) -> str:
        return self.model

    def _build_client(self) -> OpenAI:
        if not self._base_url and not self._api_key:
            raise LLMError(
                "Nenhuma credencial de LLM configurada. Defina LLM_API_KEY para usar a "
                "OpenAI, ou LLM_BASE_URL para um endpoint compatível (Ollama, vLLM, "
                "OpenRouter...). Ver .env.example."
            )
        return OpenAI(
            api_key=self._api_key or "not-needed",
            base_url=self._base_url,
        )

    def _invoke(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> RawCompletion:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt + self._schema_instruction(schema)},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": self._response_format(schema),
        }

        try:
            response = self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            # Provedor que não conhece `json_schema`: desce um degrau e não
            # tenta de novo nas próximas chamadas.
            if self._strict_supported and _rejects_json_schema(exc):
                self._strict_supported = False
                kwargs["response_format"] = {"type": "json_object"}
                response = self._client.chat.completions.create(**kwargs)
            else:
                raise

        choice = response.choices[0] if response.choices else None
        content = getattr(getattr(choice, "message", None), "content", None)
        if content is None:
            raise LLMCallError("O provedor retornou uma resposta sem conteúdo.")

        usage = getattr(response, "usage", None)
        return RawCompletion(
            raw=content,
            usage=LLMUsage(
                prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            ),
        )

    def _response_format(self, schema: dict[str, Any]) -> dict[str, Any]:
        if self._strict_supported:
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": "resposta",
                    "strict": True,
                    "schema": to_strict_schema(schema),
                },
            }
        return {"type": "json_object"}

    @staticmethod
    def _schema_instruction(schema: dict[str, Any]) -> str:
        return (
            "\n\nResponda APENAS com um JSON válido que satisfaça este JSON Schema "
            "(sem markdown, sem comentários, sem texto antes ou depois):\n"
            + json.dumps(schema, ensure_ascii=False)
        )


def _rejects_json_schema(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if status not in (400, 404, 422):
        return False
    message = str(exc).lower()
    return any(fragment in message for fragment in _SCHEMA_REJECTION_FRAGMENTS)
