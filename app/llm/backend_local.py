from __future__ import annotations

import json
import os
from typing import Type, TypeVar

from openai import OpenAI
from pydantic import BaseModel

from app.llm.client import LLMClient

T = TypeVar("T", bound=BaseModel)


class OllamaBackend(LLMClient):
    """Fala com o Ollama via sua API compatível com OpenAI — mesma interface,
    o resto do código não precisa saber que não é mais a nuvem."""

    def __init__(self, model: str | None = None):
        self.client = OpenAI(
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434/v1"),
            api_key="ollama",  # exigido pelo SDK, não é checado pelo Ollama
        )
        self.model = model or os.environ.get("OLLAMA_MODEL", "qwen2.5:14b-instruct-q4_K_M")

    def complete_structured(
        self, system_prompt: str, user_prompt: str, response_model: Type[T]
    ) -> T:
        schema_hint = (
            f"\n\nResponda APENAS com um JSON válido no seguinte formato "
            f"(sem markdown, sem comentários, sem texto antes ou depois):\n"
            f"{response_model.model_json_schema()}"
        )
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt + schema_hint},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
        )
        raw = resp.choices[0].message.content
        clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return response_model.model_validate(json.loads(clean))
