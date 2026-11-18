"""Interface comum — os agentes só conhecem isto, nunca o backend concreto."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMClient(ABC):
    @abstractmethod
    def complete_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
    ) -> T:
        """Chama o modelo e retorna a resposta já validada contra response_model.

        Implementações devem instruir o modelo a responder só em JSON e
        validar/parsear antes de retornar — nunca repassar texto livre
        para o chamador.
        """
        raise NotImplementedError
