import os

from app.llm.client import LLMClient


def get_llm_client() -> LLMClient:
    backend = os.environ.get("LLM_BACKEND", "cloud")

    if backend == "cloud":
        from app.llm.backend_cloud import AnthropicBackend
        return AnthropicBackend()

    if backend == "local":
        from app.llm.backend_local import OllamaBackend
        return OllamaBackend()

    raise ValueError(f"LLM_BACKEND inválido: {backend!r} (use 'cloud' ou 'local')")
