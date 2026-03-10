"""Abstract LLM provider interface."""

from abc import ABC, abstractmethod


class LLMProvider(ABC):
    """Base class for LLM providers."""

    @abstractmethod
    def complete(self, messages: list[dict]) -> str:
        """Run a chat completion and return the text."""

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Embed a single text."""

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts."""

    @abstractmethod
    def rerank(self, query: str, documents: list[str]) -> list[float]:
        """Rerank documents by relevance to query. Returns scores."""
