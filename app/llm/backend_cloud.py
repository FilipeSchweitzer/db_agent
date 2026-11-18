from __future__ import annotations

import json
import os
from typing import Type, TypeVar

import anthropic
from pydantic import BaseModel

from app.llm.client import LLMClient

T = TypeVar("T", bound=BaseModel)


class AnthropicBackend(LLMClient):
    def __init__(self, model: str = "claude-sonnet-4-6"):
        self.client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self.model = model

    def complete_structured(
        self, system_prompt: str, user_prompt: str, response_model: Type[T]
    ) -> T:
        schema_hint = (
            f"\n\nResponda APENAS com um JSON válido no seguinte formato "
            f"(sem markdown, sem comentários, sem texto antes ou depois):\n"
            f"{response_model.model_json_schema()}"
        )
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system_prompt + schema_hint,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = "".join(block.text for block in resp.content if block.type == "text")
        clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return response_model.model_validate(json.loads(clean))
