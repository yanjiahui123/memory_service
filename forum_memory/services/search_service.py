"""Memory search service — sync.

The 4-stage pipeline: preprocess → recall → rerank → env_match.
Uses ES hybrid search (BM25 + knn) for recall, falls back to SQL LIKE if ES unavailable.
"""

import logging
import re as re_mod
from uuid import UUID
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from forum_memory.models.memory import Memory
from forum_memory.models.namespace import Namespace
from forum_memory.models.enums import MemoryStatus, RelationType
from forum_memory.schemas.memory import MemorySearchRequest, MemorySearchResponse, MemorySearchHit, MemoryRead
from forum_memory.core.prompts import QUERY_REWRITE_SYSTEM, QUERY_REWRITE_USER
from forum_memory.providers import get_provider
from forum_memory.config import get_settings
from forum_memory.services import es_service

logger = logging.getLogger(__name__)


def search_memories(session: Session, req: MemorySearchRequest) -> MemorySearchResponse:
    """Run the full search pipeline."""
    expanded = _preprocess_query(session, req)
    candidates = _recall(session, req.namespace_id, expanded, req.top_k * 5)
    ranked = _simple_rank(candidates, expanded, req.top_k)
    hits = _build_hits(session, ranked, req.environment)
    _expand_hit_relations(session, hits)
    return MemorySearchResponse(hits=hits, query_expanded=expanded, total_recalled=len(candidates))


def find_similar(
    session: Session,
    namespace_id: UUID,
    content: str,
    top_k: int = 5,
    tags: list[str] | None = None,
    knowledge_type: str | None = None,
) -> list[dict]:
    """Find similar memories for AUDN dedup via multi-dimensional recall.

    Recall strategy: KNN top-k UNION same-tags UNION same-knowledge_type,
    then deduplicate and return up to top_k results.
    """
    ns = session.get(Namespace, namespace_id)
    es_index = ns.es_index_name if ns else None
    seen_ids: set[str] = set()
    results: list[dict] = []

    def _add_memories(memory_ids: list[UUID]) -> None:
        """Fetch memories by IDs and add unseen ones to results."""
        if not memory_ids:
            return
        new_ids = [mid for mid in memory_ids if str(mid) not in seen_ids]
        if not new_ids:
            return
        stmt = select(Memory).where(Memory.id.in_(new_ids))
        memories_map = {str(m.id): m for m in session.exec(stmt).all()}
        for mid in new_ids:
            m = memories_map.get(str(mid))
            if m:
                seen_ids.add(str(m.id))
                results.append({"id": str(m.id), "content": m.content, "authority": m.authority})

    # Try ES-based multi-dimensional recall
    try:
        provider = get_provider()
        content_embedding = provider.embed(content)

        # 1. KNN recall (primary)
        knn_hits = es_service.knn_search(
            namespace_id=namespace_id,
            query_embedding=content_embedding,
            limit=top_k,
            index_name=es_index,
        )
        _add_memories([UUID(h["memory_id"]) for h in knn_hits])

        # 2. Same-tags recall (if tags provided)
        if tags:
            tag_hits = es_service.term_search(
                namespace_id=namespace_id,
                field="tags",
                values=tags,
                limit=top_k,
                index_name=es_index,
            )
            _add_memories([UUID(h["memory_id"]) for h in tag_hits])

        # 3. Same knowledge_type recall (if provided)
        if knowledge_type:
            kt_hits = es_service.term_search(
                namespace_id=namespace_id,
                field="knowledge_type",
                values=[knowledge_type],
                limit=top_k,
                index_name=es_index,
            )
            _add_memories([UUID(h["memory_id"]) for h in kt_hits])

        if results:
            return results[:top_k]
    except Exception:
        logger.exception("ES find_similar failed, falling back to text overlap")

    # Fallback: SQL + text_overlap
    stmt = (
        select(Memory)
        .where(Memory.namespace_id == namespace_id, Memory.status == MemoryStatus.ACTIVE)
        .limit(top_k * 10)
    )
    memories = list(session.exec(stmt).all())
    results = []
    for m in memories:
        if _text_overlap(content, m.content) > 0.2:
            results.append({"id": str(m.id), "content": m.content, "authority": m.authority})
    return results[:top_k]


def _preprocess_query(session: Session, req: MemorySearchRequest) -> str:
    ns = session.get(Namespace, req.namespace_id)
    if not ns or not ns.dictionary:
        query = req.query
        dictionary = {}
    else:
        query = _apply_dictionary(req.query, ns.dictionary)
        dictionary = ns.dictionary

    # Skip LLM rewrite for short queries (≤ 4 words): no benefit, saves latency
    if len(query.split()) <= 4:
        return query

    # LLM query rewrite for better recall (longer, complex queries only)
    try:
        provider = get_provider()
        rewritten = provider.complete(
            [
                {"role": "system", "content": QUERY_REWRITE_SYSTEM},
                {"role": "user", "content": QUERY_REWRITE_USER.format(
                    query=query, dictionary=dictionary,
                )},
            ],
        )
        if rewritten and rewritten.strip():
            return rewritten.strip()
    except Exception:
        pass  # fallback to dictionary-only result
    return query


def _apply_dictionary(query: str, dictionary: dict) -> str:
    result = query
    for slang, canonical in dictionary.items():
        result = re_mod.sub(re_mod.escape(slang), canonical, result, flags=re_mod.IGNORECASE)
    return result


def _recall(session: Session, ns_id: UUID, query: str, limit: int) -> list[Memory]:
    """Recall candidates via ES hybrid search, fallback to SQL LIKE."""
    ns = session.get(Namespace, ns_id)
    es_index = ns.es_index_name if ns else None
    # Try ES hybrid search
    try:
        provider = get_provider()
        query_embedding = provider.embed(query)
        es_hits = es_service.hybrid_search(
            namespace_id=ns_id,
            query_text=query,
            query_embedding=query_embedding,
            limit=limit,
            index_name=es_index,
        )
        if es_hits:
            memory_ids = [UUID(h["memory_id"]) for h in es_hits]
            return _fetch_memories_by_ids(session, memory_ids)
    except Exception:
        logger.exception("ES recall failed, falling back to SQL")

    # Fallback: SQL LIKE with OR logic for better recall
    from sqlalchemy import or_
    stmt = (
        select(Memory)
        .where(Memory.namespace_id == ns_id, Memory.status == MemoryStatus.ACTIVE)
        .limit(limit)
    )
    keywords = query.split()[:5]
    if keywords:
        stmt = stmt.where(or_(*(Memory.content.contains(kw) for kw in keywords)))
    return list(session.exec(stmt).all())


def _fetch_memories_by_ids(session: Session, memory_ids: list[UUID]) -> list[Memory]:
    """Fetch Memory objects by IDs, preserving ES ranking order."""
    if not memory_ids:
        return []
    stmt = select(Memory).where(Memory.id.in_(memory_ids))
    memories_map = {m.id: m for m in session.exec(stmt).all()}
    return [memories_map[mid] for mid in memory_ids if mid in memories_map]


def _simple_rank(candidates: list[Memory], query: str, top_k: int) -> list[Memory]:
    """Rank candidates: 70% semantic rerank + 30% quality_score fusion, fallback to text overlap."""
    if not candidates:
        return []
    try:
        provider = get_provider()
        docs = [m.content for m in candidates]
        scores = provider.rerank(query, docs)
        # Normalize semantic scores to [0, 1] to make them comparable with quality_score
        min_s, max_s = min(scores), max(scores)
        span = max_s - min_s or 1.0
        norm_scores = [(s - min_s) / span for s in scores]
        # Fuse: 70% semantic relevance + 30% memory quality
        fused = [0.7 * sem + 0.3 * m.quality_score for m, sem in zip(candidates, norm_scores)]
        scored = list(zip(candidates, fused))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [m for m, _ in scored[:top_k]]
    except Exception:
        # fallback to text overlap
        scored = [(m, _text_overlap(query, m.content)) for m in candidates]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [m for m, _ in scored[:top_k]]


def _text_overlap(a: str, b: str) -> float:
    tokens_a = set(a.lower().split())
    tokens_b = set(b.lower().split())
    if not tokens_a or not tokens_b:
        return 0.0
    inter = tokens_a & tokens_b
    return len(inter) / max(len(tokens_a), len(tokens_b))


def _build_hits(session: Session, memories: list[Memory], env: str | None) -> list[MemorySearchHit]:
    now = datetime.now(tz=timezone(timedelta(hours=8)))
    hits = []
    memory_ids = []
    for m in memories:
        memory_ids.append(m.id)
        env_match = _check_env(m.environment, env)
        warning = None if env_match else "环境不匹配，请确认适用性"
        hit = MemorySearchHit(
            memory=MemoryRead.model_validate(m),
            score=m.quality_score,
            env_match=env_match,
            env_warning=warning,
        )
        hits.append(hit)
    # Batch update retrieval stats using SQL expression to avoid lost-update under concurrency
    if memory_ids:
        from sqlalchemy import update as sa_update
        session.execute(
            sa_update(Memory)
            .where(Memory.id.in_(memory_ids))
            .values(
                retrieve_count=Memory.retrieve_count + 1,
                last_retrieved_at=now,
            )
        )
        session.commit()
    return hits


def _check_env(mem_env: str | None, req_env: str | None) -> bool:
    if not req_env or not mem_env:
        return True
    return req_env.lower() in mem_env.lower()


# ---------------------------------------------------------------------------
# Relation expansion
# ---------------------------------------------------------------------------

_RELATION_LABELS = {
    "SUPPLEMENTS": "相关补充",
    "CONTRADICTS": "存在争议",
    "SUPERSEDES": "已被取代",
    "CAUSED_BY": "因果关联",
}


def _expand_hit_relations(session: Session, hits: list[MemorySearchHit]) -> None:
    """Enrich top search hits with related memory hints (spreading activation)."""
    if not hits:
        return
    from forum_memory.services.relation_service import expand_relations_for_memories
    from forum_memory.schemas.memory import RelatedMemoryHint

    top_ids = [h.memory.id for h in hits[:5]]
    relations_map = expand_relations_for_memories(session, top_ids)
    if not relations_map:
        return

    related_ids: set[UUID] = set()
    for rels in relations_map.values():
        for rel in rels:
            related_ids.add(rel.target_memory_id)
    detail_map = _fetch_memory_details(session, list(related_ids))

    for hit in hits[:5]:
        _attach_hints(hit, relations_map, detail_map)


def _attach_hints(
    hit: MemorySearchHit,
    relations_map: dict,
    detail_map: dict[UUID, dict],
) -> None:
    """Attach relation hints to a single search hit."""
    from forum_memory.schemas.memory import RelatedMemoryHint

    rels = relations_map.get(hit.memory.id, [])
    for rel in rels[:3]:
        details = detail_map.get(rel.target_memory_id)
        if not details:
            continue
        label = _RELATION_LABELS.get(rel.relation_type.value, rel.relation_type.value)
        # CONTRADICTS: full content for AI context; others: 100-char preview
        content = details["content"]
        preview = content if rel.relation_type == RelationType.CONTRADICTS else content[:100]
        hint = RelatedMemoryHint(
            relation_type=rel.relation_type.value,
            label=label,
            memory_id=rel.target_memory_id,
            content_preview=preview,
            confidence=rel.confidence,
            authority=details["authority"],
        )
        hit.related.append(hint)


def _fetch_memory_details(session: Session, memory_ids: list[UUID]) -> dict[UUID, dict]:
    """Fetch memory content and authority by IDs for relation hints."""
    if not memory_ids:
        return {}
    stmt = select(Memory.id, Memory.content, Memory.authority).where(
        Memory.id.in_(memory_ids)
    )
    rows = session.exec(stmt).all()
    return {row[0]: {"content": row[1], "authority": row[2]} for row in rows}
