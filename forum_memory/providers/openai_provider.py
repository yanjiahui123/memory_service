"""OpenAI LLM provider (synchronous)."""

from openai import OpenAI

from forum_memory.providers.base import LLMProvider
from forum_memory.config import get_settings


class OpenAIProvider(LLMProvider):
    """OpenAI-based LLM provider using sync client."""

    def __init__(self):
        settings = get_settings()
        self.client = OpenAI(api_key=settings.llm_api_key, timeout=settings.llm_timeout)
        self.main_model = settings.llm_main_model
        self.embed_model = settings.llm_embedding_model

    def complete(self, messages: list[dict]) -> str:
        resp = self.client.chat.completions.create(
            model=self.main_model,
            messages=messages,
            temperature=0.2,
        )
        return resp.choices[0].message.content or ""

    def embed(self, text: str) -> list[float]:
        resp = self.client.embeddings.create(
            model=self.embed_model,
            input=[text],
        )
        return resp.data[0].embedding

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        resp = self.client.embeddings.create(
            model=self.embed_model,
            input=texts,
        )
        return [d.embedding for d in resp.data]

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        """Fallback rerank using embedding cosine similarity."""
        import math
        q_emb = self.embed(query)
        d_embs = self.embed_batch(documents)
        scores = []
        for d_emb in d_embs:
            dot = sum(a * b for a, b in zip(q_emb, d_emb))
            norm_q = math.sqrt(sum(a * a for a in q_emb))
            norm_d = math.sqrt(sum(b * b for b in d_emb))
            scores.append(dot / (norm_q * norm_d) if norm_q and norm_d else 0.0)
        return scores
