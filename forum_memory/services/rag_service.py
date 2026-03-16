"""RAG knowledge base service — calls external RAG API."""

import json
import logging

import requests

from forum_memory.config import get_settings

logger = logging.getLogger(__name__)


def _find_chunks_in_dict(data: dict) -> list | None:
    """Find a list-of-chunks field in the RAG response dict."""
    list_keys = ("results", "documents", "chunks", "data", "context", "items", "records")
    for key in list_keys:
        if key in data and isinstance(data[key], list):
            logger.info("RAG chunks found under key=%r, count=%d", key, len(data[key]))
            return data[key]
    # Fallback: pick first list-typed value
    for key, val in data.items():
        if isinstance(val, list):
            logger.info("RAG chunks found under fallback key=%r, count=%d", key, len(val))
            return val
    return None


def _extract_source(item: dict) -> str | None:
    """Extract source path from chunk metadata."""
    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        return metadata.get("source")
    return None


def _format_chunk_text(item: dict) -> str:
    """Format a single chunk dict with its source for LLM prompt."""
    text = item.get("text", item.get("content", str(item)))
    source = _extract_source(item)
    if source:
        return f"[来源: {source}]\n{text}"
    return text


def _format_chunks(chunks: list) -> tuple[str, str | None]:
    """Format a list of chunks into prompt text and optional JSON for UI display."""
    parts = []
    for item in chunks:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            parts.append(_format_chunk_text(item))
    prompt_text = "\n\n".join(parts)
    structured = [c for c in chunks if isinstance(c, dict) and "text" in c]
    chunks_json = json.dumps(structured, ensure_ascii=False) if structured else None
    logger.info("RAG structured chunks stored: %d", len(structured) if structured else 0)
    return prompt_text, chunks_json


def _parse_dict_response(data: dict) -> tuple[str, str | None]:
    """Parse a dict-shaped RAG response, trying scalar fields then chunk lists."""
    for key in ("answer", "result", "text", "content"):
        if key in data and isinstance(data[key], str):
            return data[key], None

    chunks = _find_chunks_in_dict(data)
    if chunks is not None:
        return _format_chunks(chunks)

    return str(data), None


def _parse_rag_response(data) -> tuple[str, str | None]:
    """Parse an arbitrary RAG API response into (prompt_text, chunks_json)."""
    if isinstance(data, str):
        return data, None
    if isinstance(data, list):
        return _format_chunks(data)
    if isinstance(data, dict):
        return _parse_dict_response(data)
    return str(data), None


def query_rag(kb_sn_list: list[str], question: str, uid: str = "forum_memory") -> tuple[str, str | None]:
    """Query external RAG API with knowledge base serial numbers.

    Returns (prompt_text, chunks_json). Empty string on failure.
    """
    settings = get_settings()
    if not settings.rag_base_url or not kb_sn_list:
        return "", None

    try:
        resp = requests.post(
            settings.rag_base_url,
            headers={"Content-Type": "application/json"},
            json={"kb_sn_list": kb_sn_list, "question": question, "uid": uid},
            timeout=settings.rag_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        logger.info("RAG API response keys: %s", list(data.keys()) if isinstance(data, dict) else type(data).__name__)
        logger.debug("RAG API response: %s", json.dumps(data, ensure_ascii=False)[:500])
        return _parse_rag_response(data)
    except Exception:
        logger.exception("RAG query failed (non-fatal)")
        return "", None
