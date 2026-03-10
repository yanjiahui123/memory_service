"""Memory CRUD and lifecycle service — sync."""

import logging
import time
from uuid import UUID
from datetime import datetime, timezone

from sqlmodel import Session, select

from forum_memory.models.memory import Memory
from forum_memory.models.namespace import Namespace
from forum_memory.models.operation_log import OperationLog
from forum_memory.models.enums import Authority, MemoryStatus, OperationType, AUDNAction
from forum_memory.core.quality import compute_quality_score
from forum_memory.core.audn import AUDNResult
from forum_memory.schemas.memory import MemoryCreate, MemoryUpdate
from forum_memory.services import es_service

logger = logging.getLogger(__name__)


def _resolve_es_index(session: Session, namespace_id: UUID) -> str | None:
    """Look up the namespace's ES index name. Returns None if not set."""
    ns = session.get(Namespace, namespace_id)
    return ns.es_index_name if ns else None


def _index_to_es(memory: Memory, index_name: str | None = None, max_retries: int = 3) -> bool:
    """Generate embedding and index to ES. Retries on transient failure.

    Returns True on success, False after all retries exhausted.
    """
    for attempt in range(1, max_retries + 1):
        try:
            from forum_memory.providers import get_provider
            provider = get_provider()
            embedding = provider.embed(memory.content)
            success = es_service.index_memory(
                memory_id=memory.id,
                namespace_id=memory.namespace_id,
                content=memory.content,
                embedding=embedding,
                status=memory.status,
                environment=memory.environment,
                tags=memory.tags,
                knowledge_type=memory.knowledge_type,
                quality_score=memory.quality_score,
                index_name=index_name,
            )
            if success:
                return True
            raise RuntimeError("es_service.index_memory returned False")
        except Exception:
            if attempt < max_retries:
                delay = 2 ** (attempt - 1)  # 1s, 2s
                logger.warning(
                    "ES index attempt %d/%d failed for memory %s, retrying in %ds",
                    attempt, max_retries, memory.id, delay,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "ES index FAILED after %d attempts for memory %s — "
                    "DB record exists but NOT searchable. "
                    "Run reindex script to fix.",
                    max_retries, memory.id,
                )
    return False


def list_memories(
    session: Session,
    namespace_id: UUID | None = None,
    authority: str | None = None,
    status: str | None = None,
    pending_confirm: bool | None = None,
    knowledge_type: str | None = None,
    tags: str | None = None,
    q: str | None = None,
    page: int = 1,
    size: int = 20,
    source_id: UUID | None = None,
) -> list[Memory]:
    stmt = (
        select(Memory)
        .join(Namespace, Memory.namespace_id == Namespace.id)
        .where(Namespace.is_active == True)
        .where(Memory.status != MemoryStatus.DELETED)
        .order_by(Memory.updated_at.desc())
    )
    stmt = _apply_filters(stmt, namespace_id, authority, status, pending_confirm, knowledge_type, tags, q, source_id=source_id)
    stmt = stmt.offset((page - 1) * size).limit(size)
    return list(session.exec(stmt).all())


def get_memory(session: Session, memory_id: UUID) -> Memory | None:
    return session.get(Memory, memory_id)


def create_memory(session: Session, data: MemoryCreate) -> Memory:
    create_data = data.model_dump(exclude={"authority", "pending_human_confirm"})
    memory = Memory(**create_data)
    # Apply optional authority/pending from schema
    if data.authority:
        memory.authority = Authority(data.authority)
    if data.pending_human_confirm:
        memory.pending_human_confirm = data.pending_human_confirm
    # Compute initial quality score before commit
    memory.quality_score = compute_quality_score(
        useful=0, not_useful=0, wrong=0, outdated=0,
        source_role=memory.source_role,
        retrieve_count=0,
        created_at=datetime.now(timezone.utc),
        cite_count=0,
        resolved_citation_count=0,
    )
    session.add(memory)
    _add_log(session, memory, OperationType.ADD, reason="created")
    session.commit()
    session.refresh(memory)
    # ES indexing: outside transaction — failure tracked via indexed_at
    index_name = _resolve_es_index(session, memory.namespace_id)
    if _index_to_es(memory, index_name=index_name):
        memory.indexed_at = datetime.now(timezone.utc)
        session.commit()
    return memory


def update_memory(session: Session, memory_id: UUID, data: MemoryUpdate) -> Memory | None:
    memory = session.get(Memory, memory_id)
    if not memory:
        return None
    before = _snapshot(memory)
    for key, val in data.model_dump(exclude_unset=True).items():
        setattr(memory, key, val)
    memory.updated_at = datetime.now(timezone.utc)
    memory.indexed_at = None  # Mark ES as stale
    _add_log(session, memory, OperationType.UPDATE, reason="manual_update", before=before)
    session.commit()
    session.refresh(memory)
    # Re-index to ES
    index_name = _resolve_es_index(session, memory.namespace_id)
    if _index_to_es(memory, index_name=index_name):
        memory.indexed_at = datetime.now(timezone.utc)
        session.commit()
    return memory


def delete_memory(session: Session, memory_id: UUID) -> bool:
    memory = session.get(Memory, memory_id)
    if not memory:
        return False
    index_name = _resolve_es_index(session, memory.namespace_id)
    memory.status = MemoryStatus.DELETED
    memory.updated_at = datetime.now(timezone.utc)
    memory.indexed_at = None
    _add_log(session, memory, OperationType.DELETE, reason="deleted")
    session.commit()
    es_service.delete_memory_doc(memory_id, index_name=index_name)
    return True


def change_authority(session: Session, memory_id: UUID, authority: str, reason: str | None = None) -> Memory | None:
    memory = session.get(Memory, memory_id)
    if not memory:
        return None
    before = _snapshot(memory)
    old = memory.authority
    memory.authority = Authority(authority)
    memory.pending_human_confirm = False
    memory.updated_at = datetime.now(timezone.utc)
    op = OperationType.PROMOTE if authority == "LOCKED" else OperationType.DEMOTE
    _add_log(session, memory, op, reason=reason or f"{old} -> {authority}", before=before)
    session.commit()
    session.refresh(memory)
    return memory


def apply_audn(session: Session, new_fact: MemoryCreate, result: AUDNResult) -> Memory | None:
    """Apply an AUDN decision to the memory store.

    For DELETE: removes the obsolete memory, then creates the new fact (REPLACE semantics).
    Returns the newly created/updated memory, or None for NONE.
    """
    if result.action == AUDNAction.ADD:
        # Flag for human review if the new fact conflicts with a LOCKED memory
        if result.conflict_with_locked:
            new_fact.pending_human_confirm = True
            logger.warning(
                "New fact conflicts with LOCKED memory %s — flagging for human review. Reason: %s",
                result.conflict_with_locked, result.reason,
            )
        memory = create_memory(session, new_fact)
        if result.conflict_with_locked and memory:
            _add_log(
                session, memory, OperationType.ADD,
                reason=f"conflict_with_locked={result.conflict_with_locked}: {result.reason}",
            )
            session.commit()
        return memory
    if result.action == AUDNAction.UPDATE:
        return _apply_update(session, result, new_fact)
    if result.action == AUDNAction.DELETE:
        _apply_delete(session, result)
        # The new fact supersedes the old one — create it after deleting the obsolete memory
        return create_memory(session, new_fact)
    return None  # NONE


def refresh_quality(session: Session, memory_id: UUID) -> float:
    from forum_memory.config import get_settings
    memory = session.get(Memory, memory_id)
    if not memory:
        return 0.0
    score = compute_quality_score(
        useful=memory.useful_count,
        not_useful=memory.not_useful_count,
        wrong=memory.wrong_count,
        outdated=memory.outdated_count,
        source_role=memory.source_role,
        retrieve_count=memory.retrieve_count,
        created_at=memory.created_at,
        cite_count=memory.cite_count,
        resolved_citation_count=memory.resolved_citation_count,
    )
    memory.quality_score = score
    memory.updated_at = datetime.now(timezone.utc)

    # 自动质量告警：wrong 反馈超阈值时标记为待复核
    settings = get_settings()
    threshold = getattr(settings, 'wrong_feedback_threshold', 3)
    if memory.wrong_count >= threshold and not memory.pending_human_confirm:
        memory.pending_human_confirm = True
        logger.warning(
            "Memory %s flagged for review: wrong_count=%d (threshold=%d), authority=%s",
            memory.id, memory.wrong_count, threshold, memory.authority,
        )

    session.commit()
    return score


def _apply_update(session: Session, result: AUDNResult,
                   new_fact: MemoryCreate | None = None) -> Memory | None:
    if not result.target_id or not result.merged_content:
        return None
    memory = session.get(Memory, UUID(result.target_id))
    if not memory:
        return None
    if memory.authority == Authority.LOCKED:
        # LOCKED memory cannot be updated; create new fact as independent entry
        # flagged for human review instead of silently dropping it
        if new_fact:
            new_fact.pending_human_confirm = True
            logger.warning(
                "AUDN wanted to UPDATE LOCKED memory %s — creating new fact for human review. Reason: %s",
                result.target_id, result.reason,
            )
            return create_memory(session, new_fact)
        return None
    before = _snapshot(memory)
    memory.content = result.merged_content
    # Merge metadata from the new fact: union tags, prefer newer knowledge_type
    if new_fact:
        if new_fact.tags:
            existing_tags = set(memory.tags or [])
            memory.tags = sorted(existing_tags | set(new_fact.tags))
        if new_fact.knowledge_type and not memory.knowledge_type:
            memory.knowledge_type = new_fact.knowledge_type
    memory.updated_at = datetime.now(timezone.utc)
    memory.indexed_at = None  # Mark ES as stale
    _add_log(session, memory, OperationType.UPDATE, reason=result.reason, before=before)
    session.commit()
    session.refresh(memory)
    # Re-index to ES
    index_name = _resolve_es_index(session, memory.namespace_id)
    if _index_to_es(memory, index_name=index_name):
        memory.indexed_at = datetime.now(timezone.utc)
        session.commit()
    return memory


def _apply_delete(session: Session, result: AUDNResult) -> None:
    """Soft-delete the target memory. Called as part of REPLACE (DELETE + ADD)."""
    if not result.target_id:
        return
    memory = session.get(Memory, UUID(result.target_id))
    if not memory:
        return
    if memory.authority == Authority.LOCKED:
        # Cannot delete LOCKED memory; flag it for human review
        # (the caller will still create the new fact, which may contradict this one)
        memory.pending_human_confirm = True
        _add_log(session, memory, OperationType.UPDATE,
                 reason=f"AUDN wanted DELETE but memory is LOCKED: {result.reason}")
        session.commit()
        logger.warning(
            "AUDN wanted to DELETE LOCKED memory %s — flagged for human review. Reason: %s",
            result.target_id, result.reason,
        )
        return
    index_name = _resolve_es_index(session, memory.namespace_id)
    memory.status = MemoryStatus.DELETED
    memory.updated_at = datetime.now(timezone.utc)
    memory.indexed_at = None
    _add_log(session, memory, OperationType.DELETE, reason=result.reason)
    session.commit()
    es_service.delete_memory_doc(UUID(result.target_id), index_name=index_name)


def restore_memory(session: Session, memory_id: UUID) -> Memory | None:
    """Restore a COLD or ARCHIVED memory to ACTIVE status and immediately re-index to ES.

    This eliminates the 10-minute unindexed window that would occur if relying on
    the es_sync_repair_sensor to pick up the restored memory.
    """
    memory = session.get(Memory, memory_id)
    if not memory:
        return None
    if memory.status not in (MemoryStatus.COLD, MemoryStatus.ARCHIVED):
        return memory  # Already ACTIVE or DELETED, no-op

    before = _snapshot(memory)
    old_status = memory.status
    memory.status = MemoryStatus.ACTIVE
    memory.updated_at = datetime.now(timezone.utc)
    memory.indexed_at = None  # Will be set after successful ES index
    _add_log(session, memory, OperationType.UPDATE, reason=f"{old_status} → ACTIVE (restored)", before=before)
    session.commit()
    session.refresh(memory)

    # Immediately re-index to ES so the memory is searchable right away
    index_name = _resolve_es_index(session, memory.namespace_id)
    if _index_to_es(memory, index_name=index_name):
        memory.indexed_at = datetime.now(timezone.utc)
        session.commit()
    else:
        logger.warning(
            "Memory %s restored to ACTIVE but ES index failed; "
            "es_sync_repair_sensor will fix within 10 minutes",
            memory.id,
        )

    logger.info("Restored memory %s from %s to ACTIVE", memory.id, old_status)
    return memory


def transition_cold_memories(session: Session, cold_days: int = 180) -> int:
    """Transition ACTIVE memories inactive for cold_days to COLD status."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=cold_days)
    stmt = (
        select(Memory)
        .where(Memory.status == MemoryStatus.ACTIVE)
        .where(
            # Use last_retrieved_at if available, otherwise fall back to updated_at
            (Memory.last_retrieved_at < cutoff) | (
                (Memory.last_retrieved_at == None) & (Memory.updated_at < cutoff)  # noqa: E711
            )
        )
    )
    memories = list(session.exec(stmt).all())
    # Collect ES cleanup info before modifying state
    es_cleanup = []
    now = datetime.now(timezone.utc)
    for m in memories:
        before = _snapshot(m)
        es_cleanup.append((m.id, _resolve_es_index(session, m.namespace_id)))
        m.status = MemoryStatus.COLD
        m.updated_at = now
        m.indexed_at = None
        _add_log(session, m, OperationType.ARCHIVE, reason=f"inactive {cold_days}+ days → COLD", before=before)
    if memories:
        session.commit()
    # Remove from ES after successful DB commit
    for memory_id, index_name in es_cleanup:
        es_service.delete_memory_doc(memory_id, index_name=index_name)
    logger.info("Transitioned %d memories to COLD", len(memories))
    return len(memories)


def transition_archived_memories(session: Session, archive_days: int = 365) -> int:
    """Transition COLD memories inactive for archive_days to ARCHIVED status."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=archive_days)
    stmt = (
        select(Memory)
        .where(Memory.status == MemoryStatus.COLD)
        .where(Memory.updated_at < cutoff)
    )
    memories = list(session.exec(stmt).all())
    now = datetime.now(timezone.utc)
    for m in memories:
        before = _snapshot(m)
        m.status = MemoryStatus.ARCHIVED
        m.updated_at = now
        _add_log(session, m, OperationType.ARCHIVE, reason=f"inactive {archive_days}+ days → ARCHIVED", before=before)
    if memories:
        session.commit()
    logger.info("Transitioned %d memories to ARCHIVED", len(memories))
    return len(memories)


def bulk_refresh_quality(session: Session, batch_size: int = 200) -> int:
    """Refresh quality score for all ACTIVE memories in batches. Returns count updated."""
    offset = 0
    total_updated = 0
    while True:
        stmt = (
            select(Memory)
            .where(Memory.status == MemoryStatus.ACTIVE)
            .order_by(Memory.id)
            .offset(offset)
            .limit(batch_size)
        )
        memories = list(session.exec(stmt).all())
        if not memories:
            break

        from forum_memory.config import get_settings
        wrong_threshold = getattr(get_settings(), 'wrong_feedback_threshold', 3)
        changed = []
        for m in memories:
            old_score = m.quality_score
            new_score = compute_quality_score(
                useful=m.useful_count,
                not_useful=m.not_useful_count,
                wrong=m.wrong_count,
                outdated=m.outdated_count,
                source_role=m.source_role,
                retrieve_count=m.retrieve_count,
                created_at=m.created_at,
                cite_count=m.cite_count,
                resolved_citation_count=m.resolved_citation_count,
            )
            # 自动告警标记
            if m.wrong_count >= wrong_threshold and not m.pending_human_confirm:
                m.pending_human_confirm = True
            if abs(new_score - old_score) > 0.001:
                m.quality_score = new_score
                m.indexed_at = None  # Mark ES as stale
                changed.append(m)

        if changed:
            session.commit()  # Commit quality score updates

            # Batch embed: one API call for all changed content
            try:
                from forum_memory.providers import get_provider
                provider = get_provider()
                embeddings = provider.embed_batch([m.content for m in changed])
            except Exception:
                logger.exception(
                    "embed_batch failed during quality refresh at offset %d; "
                    "ES sync deferred to repair sensor",
                    offset,
                )
                total_updated += len(changed)
                offset += batch_size
                continue

            # Group by namespace ES index for bulk reindex
            # Pre-fetch all namespace_id → es_index_name in a single query to avoid N+1
            now = datetime.now(timezone.utc)
            unique_ns_ids = list({m.namespace_id for m in changed})
            ns_rows = list(session.exec(
                select(Namespace.id, Namespace.es_index_name).where(Namespace.id.in_(unique_ns_ids))
            ).all())
            ns_cache = {row[0]: row[1] for row in ns_rows}
            by_index: dict = {}  # index_name → [(Memory, embedding)]
            for m, emb in zip(changed, embeddings):
                index_name = ns_cache.get(m.namespace_id)
                by_index.setdefault(index_name, []).append((m, emb))

            for index_name, pairs in by_index.items():
                if index_name is None:
                    logger.warning(
                        "Skipping %d memories with no ES index (namespace deleted?)",
                        len(pairs),
                    )
                    continue
                docs = [
                    {
                        "memory_id": str(m.id),
                        "namespace_id": str(m.namespace_id),
                        "content": m.content,
                        "embedding": emb,
                        "status": m.status,
                        "environment": m.environment or "",
                        "tags": m.tags or [],
                        "knowledge_type": m.knowledge_type or "",
                        "quality_score": m.quality_score,
                    }
                    for m, emb in pairs
                ]
                ok, failed_ids = es_service.bulk_reindex(docs, index_name=index_name)
                # Mark indexed_at per-item: only for successfully indexed memories
                for m, _ in pairs:
                    if str(m.id) not in failed_ids:
                        m.indexed_at = now
                if failed_ids:
                    logger.warning(
                        "Partial bulk reindex (%d/%d) for index %s; "
                        "failed IDs will be repaired by es_sync_repair_sensor",
                        ok, len(docs), index_name,
                    )

            if any(m.indexed_at is not None for m in changed):
                session.commit()

            total_updated += len(changed)

        offset += batch_size
    logger.info("Refreshed quality for %d memories", total_updated)
    return total_updated


def list_all_tags(
    session: Session,
    namespace_id: UUID | None = None,
    min_count: int = 2,
) -> list[str]:
    """Return tags sorted by frequency (descending), filtered by min_count.

    Uses PostgreSQL jsonb_array_elements_text for efficient SQL-level aggregation
    instead of loading all rows into Python memory.
    """
    from sqlalchemy import text as sa_text

    # Use jsonb_array_elements_text to unnest tags array in SQL
    tag_unnest = sa_text(
        "SELECT jsonb_array_elements_text(tags) AS tag "
        "FROM memories "
        "WHERE status != 'DELETED' AND tags IS NOT NULL"
        + (" AND namespace_id = :ns_id" if namespace_id else "")
    )
    params = {"ns_id": str(namespace_id)} if namespace_id else {}

    # Wrap in a subquery to aggregate
    count_query = sa_text(
        f"SELECT t.tag, COUNT(*) AS cnt FROM ({tag_unnest.text}) t "
        f"WHERE t.tag != '' "
        f"GROUP BY t.tag HAVING COUNT(*) >= :min_count "
        f"ORDER BY cnt DESC, t.tag"
    )
    params["min_count"] = min_count

    rows = session.execute(count_query, params).all()
    return [row[0] for row in rows]


def batch_get_memories(session: Session, ids: list[UUID]) -> list[Memory]:
    """Fetch multiple memories by IDs."""
    if not ids:
        return []
    stmt = select(Memory).where(Memory.id.in_(ids))
    return list(session.exec(stmt).all())


def count_memories(
    session: Session,
    namespace_id: UUID | None = None,
    authority: str | None = None,
    status: str | None = None,
    pending_confirm: bool | None = None,
    knowledge_type: str | None = None,
    tags: str | None = None,
    q: str | None = None,
    source_id: UUID | None = None,
) -> int:
    """Count memories matching the given filters (for pagination)."""
    from sqlmodel import func
    stmt = (
        select(func.count())
        .select_from(Memory)
        .join(Namespace, Memory.namespace_id == Namespace.id)
        .where(Namespace.is_active == True)
        .where(Memory.status != MemoryStatus.DELETED)
    )
    stmt = _apply_filters(stmt, namespace_id, authority, status, pending_confirm, knowledge_type, tags, q, source_id=source_id)
    return session.exec(stmt).one()


def _apply_filters(stmt, ns_id, authority, status, pending, knowledge_type=None, tags=None, q=None, source_id=None):
    if ns_id:
        stmt = stmt.where(Memory.namespace_id == ns_id)
    if authority:
        stmt = stmt.where(Memory.authority == authority)
    if status:
        stmt = stmt.where(Memory.status == status)
    if pending:
        stmt = stmt.where(Memory.pending_human_confirm == True)
    if knowledge_type:
        stmt = stmt.where(Memory.knowledge_type == knowledge_type)
    if tags:
        # Filter memories using PostgreSQL JSONB @> operator for exact tag matching
        from sqlalchemy import text as sa_text, literal_column
        for tag in tags.split(","):
            tag = tag.strip()
            if tag:
                # Use JSONB @> operator: tags @> '["tag_value"]'::jsonb
                import json
                stmt = stmt.where(literal_column("memories.tags").op("@>")(sa_text(f"'{json.dumps([tag])}'::jsonb")))
    if q:
        stmt = stmt.where(Memory.content.ilike(f"%{q}%"))
    if source_id:
        stmt = stmt.where(Memory.source_id == source_id)
    return stmt


def _snapshot(memory: Memory) -> dict:
    return {"content": memory.content, "authority": memory.authority, "status": memory.status}


def reindex_unsynced_memories(session: Session, batch_size: int = 50) -> int:
    """Find ACTIVE memories with indexed_at IS NULL and re-index to ES.

    This is a repair function for DB-ES consistency gaps — called by a periodic
    Dagster job to fix memories that failed ES indexing on creation/update.
    Returns the number of successfully re-indexed memories.
    """
    stmt = (
        select(Memory)
        .where(Memory.status == MemoryStatus.ACTIVE)
        .where(Memory.indexed_at == None)  # noqa: E711
        .order_by(Memory.created_at)  # deterministic order so repeated runs make progress
        .limit(batch_size)
    )
    memories = list(session.exec(stmt).all())
    if not memories:
        return 0

    now = datetime.now(timezone.utc)
    succeeded = []
    for m in memories:
        index_name = _resolve_es_index(session, m.namespace_id)
        if _index_to_es(m, index_name=index_name):
            m.indexed_at = now
            succeeded.append(m)
        else:
            logger.warning("Repair reindex still failed for memory %s", m.id)

    if succeeded:
        session.commit()

    logger.info("Repair reindex: %d/%d memories synced to ES", len(succeeded), len(memories))
    return len(succeeded)


def _add_log(session: Session, memory: Memory, op: OperationType, reason: str | None = None, before: dict | None = None) -> None:
    """Add an operation log to the current session (caller is responsible for commit)."""
    log = OperationLog(
        memory_id=memory.id,
        operation=op,
        operator_type="system",
        reason=reason,
        before_snapshot=before,
    )
    session.add(log)
