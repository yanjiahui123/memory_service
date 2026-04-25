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
        self.headers = {
            "X-HW-ID": settings.app_id,
            "X-HW-APPKEY": settings.app_key
        }
        if settings.custom_api_key:
            self.headers["Authorization"] = f"Bearer {settings.custom_api_key}"
        self.llm_model = settings.custom_llm_model
        self.embed_model = settings.custom_embed_model
        self.rerank_model = settings.custom_rerank_model
        self.embed_dimension = settings.embedding_dimension
        self.timeout = settings.llm_timeout
        # Vision model (optional)
        self.vision_url = settings.custom_vision_url or self.llm_url
        self.vision_model = settings.custom_vision_model
        self.vision_enabled = settings.vision_enabled

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

    def describe_image(self, image_url: str) -> str:
        """Describe image content via vision model (Qwen2.5-VL compatible).

        Returns raw VL model output. The expected format (enforced by prompt):
            描述: <detailed description>
            关键词: <keyword1>, <keyword2>, ...
        Parsing is handled by the caller (image_preprocessor).
        """
        if not self.vision_enabled or not self.vision_model:
            raise NotImplementedError("Vision model not configured")
        messages = _build_vision_messages(image_url)
        resp = requests.post(
            self.vision_url,
            headers=self.headers,
            json={"model": self.vision_model, "messages": messages},
            verify=False,
        )
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return resp.json()["choices"][0]["message"]["content"]

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        texts = [[query, doc] for doc in documents]
        resp = requests.post(
            self.rerank_url,
            headers=self.headers,
            json={
                "model": self.rerank_model,
                "sentence_pairs": texts,
                "dimensions": self.embed_dimension,
                "enable_instruct": True,
            },
            verify=False,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()


_VISION_PROMPT = (
    "请详细描述这张图片的内容。如果包含文字、代码、错误信息、"
    "表格或配置，请完整提取。如果是架构图或流程图，"
    "请描述各个组件及其关系。\n\n"
    "请严格按以下格式输出（两行）：\n"
    "描述: <完整描述>\n"
    "关键词: <3-5个用于搜索的关键词，逗号分隔>"
)


def _build_vision_messages(image_url: str) -> list[dict]:
    """Build OpenAI-compatible multimodal messages for the vision model."""
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _VISION_PROMPT},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        }
    ]


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
