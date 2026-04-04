"""Custom LLM provider using internal HTTP APIs."""

import json
import urllib3
import requests
from collections.abc import Iterator

from forum_memory.providers.base import LLMProvider
from forum_memory.config import get_settings

# Suppress InsecureRequestWarning for verify=False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class CustomProvider(LLMProvider):
    """Provider backed by custom internal LLM/Embedding/Rerank HTTP endpoints."""

    def __init__(self):
        settings = get_settings()
        self.llm_url = settings.custom_llm_url
        self.embed_url = settings.custom_embed_url
        self.rerank_url = settings.custom_rerank_url
        self.headers = {}
        if settings.custom_api_key:
            self.headers["Authorization"] = f"Bearer {settings.custom_api_key}"
        self.llm_model = settings.custom_llm_model
        self.embed_model = settings.custom_embed_model
        self.rerank_model = settings.custom_rerank_model
        self.embed_dimension = settings.embedding_dimension
        self.timeout = settings.llm_timeout

    def complete(self, messages: list[dict]) -> str:
        resp = requests.post(
            self.llm_url,
            headers=self.headers,
            json={"model": self.llm_model, "messages": messages},
            verify=False,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return resp.json()["choices"][0]["message"]["content"]

    def complete_stream(self, messages: list[dict]) -> Iterator[str]:
        resp = requests.post(
            self.llm_url,
            headers={**self.headers, "Accept": "text/event-stream"},
            json={"model": self.llm_model, "messages": messages, "stream": True},
            verify=False,
            timeout=self.timeout,
            stream=True,
        )
        resp.raise_for_status()
        # 强制 UTF-8：很多内部 LLM 服务响应头缺少 charset，
        # requests 会 fallback 到 ISO-8859-1 导致中文乱码
        resp.encoding = "utf-8"
        yield from _iter_sse_tokens(resp)

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        resp = requests.post(
            self.embed_url,
            headers=self.headers,
            json={
                "model": self.embed_model,
                "texts": texts,
                "dimensions": self.embed_dimension,
            },
            verify=False,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        texts = [[query, doc] for doc in documents]
        resp = requests.post(
            self.rerank_url,
            headers=self.headers,
            json={
                "model": self.rerank_model,
                "texts": texts,
                "dimensions": self.embed_dimension,
                "enable_instruct": True,
            },
            verify=False,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()


def _iter_sse_tokens(resp: requests.Response) -> Iterator[str]:
    """Parse OpenAI-compatible SSE stream, yielding content deltas."""
    for raw_line in resp.iter_lines(decode_unicode=True):
        if not raw_line or not raw_line.startswith("data:"):
            continue
        payload = raw_line[len("data:"):].strip()
        if payload == "[DONE]":
            return
        try:
            chunk = json.loads(payload)
            delta = chunk["choices"][0]["delta"].get("content")
            if delta:
                yield delta
        except (json.JSONDecodeError, KeyError, IndexError):
            continue
