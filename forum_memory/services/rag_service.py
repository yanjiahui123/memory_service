"""RAG knowledge base service — calls external RAG API."""

import json
import logging

import requests

from forum_memory.config import get_settings

logger = logging.getLogger(__name__)


def query_rag(kb_sn_list: list[str], question: str, uid: str = "forum_memory") -> tuple[str, str | None]:
    """
    Query external RAG API with knowledge base serial numbers.

    Returns a tuple of:
      - prompt_text: formatted text for LLM prompt (empty string on failure)
      - chunks_json: JSON-serialized list of raw chunks for UI display, or None if
                     the API did not return structured chunk data
    """
    settings = get_settings()
    if not settings.rag_base_url or not kb_sn_list:
        return "", None

    try:
        resp = requests.post(
            settings.rag_base_url,
            headers={"Content-Type": "application/json"},
            json={
                "kb_sn_list": kb_sn_list,
                "question": question,
                "uid": uid,
            },
            timeout=settings.rag_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        logger.info("RAG API response keys: %s", list(data.keys()) if isinstance(data, dict) else type(data).__name__)
        logger.debug("RAG API response: %s", json.dumps(data, ensure_ascii=False)[:500])

        # If the API returned a plain string
        if isinstance(data, str):
            return data, None

        if isinstance(data, dict):
            # Try scalar answer fields first
            for key in ("answer", "result", "text", "content"):
                if key in data and isinstance(data[key], str):
                    return data[key], None

            # Try named list-of-chunks fields
            list_keys = ("results", "documents", "chunks", "data", "context", "items", "records")
            chunks = None
            for key in list_keys:
                if key in data and isinstance(data[key], list):
                    chunks = data[key]
                    logger.info("RAG chunks found under key=%r, count=%d", key, len(chunks))
                    break

            # Fallback: pick first list-typed value in the response
            if chunks is None:
                for key, val in data.items():
                    if isinstance(val, list):
                        chunks = val
                        logger.info("RAG chunks found under fallback key=%r, count=%d", key, len(chunks))
                        break

            if chunks is not None:
                parts = []
                for item in chunks:
                    if isinstance(item, str):
                        parts.append(item)
                    elif isinstance(item, dict):
                        parts.append(item.get("text", item.get("content", str(item))))
                prompt_text = "\n\n".join(parts)
                structured = [c for c in chunks if isinstance(c, dict) and "text" in c]
                chunks_json = json.dumps(structured, ensure_ascii=False) if structured else None
                logger.info("RAG structured chunks stored: %d", len(structured) if structured else 0)
                return prompt_text, chunks_json

        return str(data), None
    except Exception:
        logger.exception("RAG query failed (non-fatal)")
        return "", None
