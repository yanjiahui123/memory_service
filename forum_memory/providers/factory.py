"""LLM provider factory."""

from forum_memory.providers.base import LLMProvider
from forum_memory.config import get_settings

_instance: LLMProvider | None = None


def get_provider() -> LLMProvider:
    """Return the configured LLM provider (singleton)."""
    global _instance
    if _instance is not None:
        return _instance

    settings = get_settings()
    if settings.llm_provider == "openai":
        from forum_memory.providers.openai_provider import OpenAIProvider
        _instance = OpenAIProvider()
    elif settings.llm_provider == "custom":
        from forum_memory.providers.custom_provider import CustomProvider
        _instance = CustomProvider()
    else:
        raise ValueError(f"Unknown LLM provider: {settings.llm_provider}")
    return _instance
