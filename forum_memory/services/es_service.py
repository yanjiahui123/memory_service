"""Elasticsearch service — index management, CRUD, hybrid search."""

import logging
from uuid import UUID

from elasticsearch import Elasticsearch, NotFoundError
from elasticsearch.helpers import bulk

from forum_memory.config import get_settings

logger = logging.getLogger(__name__)

_client: Elasticsearch | None = None


# ── Client & Index ───────────────────────────────────────

def get_es_client() -> Elasticsearch | None:
    """Return ES client singleton, or None if disabled."""
    global _client
    settings = get_settings()
    if not settings.es_enabled:
        return None
    if _client is not None:
        return _client

    kwargs: dict = {"hosts": [settings.es_url], "verify_certs": settings.es_verify_certs}
    if settings.es_username:
        kwargs["basic_auth"] = (settings.es_username, settings.es_password)

    _client = Elasticsearch(**kwargs)
    return _client


def _default_index_name() -> str:
    return f"{get_settings().es_index_prefix}_memories"


def _detect_analyzer(es: Elasticsearch, settings_cfg) -> str:
    """Detect best available analyzer: prefer ik_max_word for Chinese, fall back to standard."""
    preferred = getattr(settings_cfg, "es_content_analyzer", "ik_max_word")
    if preferred == "standard":
        return "standard"
    try:
        # Test if the analyzer plugin is installed
        es.indices.analyze(body={"analyzer": preferred, "text": "测试"})
        logger.info("Using ES analyzer: %s", preferred)
        return preferred
    except Exception:
        logger.warning(
            "ES analyzer '%s' not available (IK plugin not installed?), falling back to 'standard'",
            preferred,
        )
        return "standard"


def ensure_index_by_name(name: str) -> None:
    """Create an ES index with correct mapping if it doesn't exist."""
    es = get_es_client()
    if not es:
        return
    if es.indices.exists(index=name):
        return

    settings_cfg = get_settings()
    content_analyzer = _detect_analyzer(es, settings_cfg)
    body = {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
        },
        "mappings": {
            "properties": {
                "memory_id":      {"type": "keyword"},
                "namespace_id":   {"type": "keyword"},
                "content":        {"type": "text", "analyzer": content_analyzer},
                "embedding":      {
                    "type": "dense_vector",
                    "dims": settings_cfg.embedding_dimension,
                    "index": True,
                    "similarity": "cosine",
                },
                "status":         {"type": "keyword"},
                "environment":    {"type": "keyword"},
                "tags":           {"type": "keyword"},
                "knowledge_type": {"type": "keyword"},
                "quality_score":  {"type": "float"},
            }
        },
    }
    es.indices.create(index=name, body=body)
    logger.info("Created ES index: %s (dims=%d)", name, settings_cfg.embedding_dimension)


def ensure_index() -> None:
    """Create the default global ES index."""
    ensure_index_by_name(_default_index_name())


def delete_index(index_name: str) -> bool:
    """Delete an ES index. Returns True on success."""
    es = get_es_client()
    if not es:
        return False
    try:
        if es.indices.exists(index=index_name):
            es.indices.delete(index=index_name)
            logger.info("Deleted ES index: %s", index_name)
        return True
    except Exception:
        logger.exception("Failed to delete ES index %s", index_name)
        return False


# ── Document CRUD ────────────────────────────────────────

def index_memory(
    memory_id: UUID,
    namespace_id: UUID,
    content: str,
    embedding: list[float],
    status: str = "ACTIVE",
    environment: str | None = None,
    tags: list | None = None,
    knowledge_type: str | None = None,
    quality_score: float = 0.5,
    index_name: str | None = None,
) -> bool:
    """Index or update a memory document. Returns True on success."""
    es = get_es_client()
    if not es:
        return False
    try:
        name = index_name or _default_index_name()
        doc = {
            "memory_id": str(memory_id),
            "namespace_id": str(namespace_id),
            "content": content,
            "embedding": embedding,
            "status": status,
            "environment": environment or "",
            "tags": tags or [],
            "knowledge_type": knowledge_type or "",
            "quality_score": quality_score,
        }
        es.index(index=name, id=str(memory_id), document=doc)
        return True
    except Exception:
        logger.exception("Failed to index memory %s", memory_id)
        return False


def delete_memory_doc(memory_id: UUID, index_name: str | None = None) -> bool:
    """Remove a memory document from ES. Returns True on success."""
    es = get_es_client()
    if not es:
        return False
    try:
        name = index_name or _default_index_name()
        es.delete(index=name, id=str(memory_id))
        return True
    except NotFoundError:
        return True  # already gone
    except Exception:
        logger.exception("Failed to delete memory %s from ES", memory_id)
        return False


# ── Search ───────────────────────────────────────────────

def hybrid_search(
    namespace_id: UUID,
    query_text: str,
    query_embedding: list[float],
    limit: int = 50,
    status_filter: str = "ACTIVE",
    index_name: str | None = None,
) -> list[dict]:
    """BM25 + knn hybrid search.

    Returns [{"memory_id": str, "score": float}, ...]
    """
    es = get_es_client()
    if not es:
        return []
    settings = get_settings()
    name = index_name or _default_index_name()

    filter_clauses = [
        {"term": {"namespace_id": str(namespace_id)}},
        {"term": {"status": status_filter}},
    ]

    try:
        resp = es.search(
            index=name,
            size=limit,
            query={
                "bool": {
                    "must": {"match": {"content": query_text}},
                    "filter": filter_clauses,
                }
            },
            knn={
                "field": "embedding",
                "query_vector": query_embedding,
                "k": limit,
                "num_candidates": settings.es_knn_num_candidates,
                "filter": {"bool": {"filter": filter_clauses}},
            },
        )
        return _parse_hits(resp)
    except Exception:
        logger.exception("ES hybrid search failed")
        return []


def knn_search(
    namespace_id: UUID,
    query_embedding: list[float],
    limit: int = 5,
    status_filter: str = "ACTIVE",
    index_name: str | None = None,
) -> list[dict]:
    """Pure knn vector search (for AUDN find_similar).

    Returns [{"memory_id": str, "score": float}, ...]
    """
    es = get_es_client()
    if not es:
        return []
    settings = get_settings()
    name = index_name or _default_index_name()

    filter_clauses = [
        {"term": {"namespace_id": str(namespace_id)}},
        {"term": {"status": status_filter}},
    ]

    try:
        resp = es.search(
            index=name,
            size=limit,
            knn={
                "field": "embedding",
                "query_vector": query_embedding,
                "k": limit,
                "num_candidates": settings.es_knn_num_candidates,
                "filter": {"bool": {"filter": filter_clauses}},
            },
        )
        return _parse_hits(resp)
    except Exception:
        logger.exception("ES knn search failed")
        return []


def term_search(
    namespace_id: UUID,
    field: str,
    values: list[str],
    limit: int = 15,
    status_filter: str = "ACTIVE",
    index_name: str | None = None,
) -> list[dict]:
    """Search by exact term match on a keyword field (e.g. tags, knowledge_type).

    Returns [{"memory_id": str, "score": float}, ...]
    """
    es = get_es_client()
    if not es or not values:
        return []
    name = index_name or _default_index_name()

    try:
        resp = es.search(
            index=name,
            size=limit,
            query={
                "bool": {
                    "must": {"terms": {field: values}},
                    "filter": [
                        {"term": {"namespace_id": str(namespace_id)}},
                        {"term": {"status": status_filter}},
                    ],
                }
            },
        )
        return _parse_hits(resp)
    except Exception:
        logger.exception("ES term_search failed for field=%s values=%s", field, values)
        return []


def _parse_hits(resp: dict) -> list[dict]:
    """Extract memory_id and score from ES response."""
    hits = resp.get("hits", {}).get("hits", [])
    return [{"memory_id": hit["_id"], "score": hit.get("_score", 0.0)} for hit in hits]


# ── Bulk ─────────────────────────────────────────────────

def bulk_reindex(memories: list[dict], batch_size: int = 100, index_name: str | None = None) -> tuple[int, set[str]]:
    """Bulk index memory dicts into ES.

    Returns (success_count, failed_memory_ids).
    Each dict: memory_id, namespace_id, content, embedding, status,
    environment, tags, knowledge_type, quality_score.
    """
    es = get_es_client()
    if not es:
        all_ids = {m["memory_id"] for m in memories}
        return 0, all_ids
    name = index_name or _default_index_name()

    actions = [
        {
            "_index": name,
            "_id": m["memory_id"],
            "_source": {
                "memory_id": m["memory_id"],
                "namespace_id": m["namespace_id"],
                "content": m["content"],
                "embedding": m["embedding"],
                "status": m["status"],
                "environment": m.get("environment") or "",
                "tags": m.get("tags") or [],
                "knowledge_type": m.get("knowledge_type") or "",
                "quality_score": m.get("quality_score", 0.5),
            },
        }
        for m in memories
    ]

    success, errors = bulk(es, actions, chunk_size=batch_size, raise_on_error=False)
    failed_ids: set[str] = set()
    if errors:
        for err in errors:
            for action_type in ("index", "create", "update"):
                if action_type in err:
                    failed_ids.add(err[action_type]["_id"])
                    break
        logger.warning("Bulk reindex had %d errors (failed IDs: %s)", len(errors), failed_ids)
    return success, failed_ids
