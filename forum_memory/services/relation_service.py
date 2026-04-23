"""Memory relation CRUD service — sync."""

import logging
from uuid import UUID

from sqlmodel import Session, select, or_

from forum_memory.models.memory_relation import MemoryRelation
from forum_memory.models.memory import Memory
from forum_memory.models.enums import RelationType, MemoryStatus, OperationType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

def create_relation(
    session: Session,
    source_memory_id: UUID,
    target_memory_id: UUID,
    relation_type: RelationType,
    confidence: float = 1.0,
    origin: str = "audn",
) -> MemoryRelation | None:
    """Create a relation if both memories exist. Idempotent (returns existing on conflict)."""
    if source_memory_id == target_memory_id:
        return None
    if not _both_memories_exist(session, source_memory_id, target_memory_id):
        return None
    existing = _find_existing(session, source_memory_id, target_memory_id, relation_type)
    if existing:
        return existing
    rel = MemoryRelation(
        source_memory_id=source_memory_id,
        target_memory_id=target_memory_id,
        relation_type=relation_type,
        confidence=confidence,
        origin=origin,
    )
    session.add(rel)
    session.commit()
    session.refresh(rel)
    return rel


def _both_memories_exist(session: Session, id_a: UUID, id_b: UUID) -> bool:
    rows = session.exec(
        select(Memory.id).where(Memory.id.in_([id_a, id_b]))
    ).all()
    return len(set(rows)) == 2


def _find_existing(
    session: Session, source_id: UUID, target_id: UUID, rel_type: RelationType,
) -> MemoryRelation | None:
    stmt = select(MemoryRelation).where(
        MemoryRelation.source_memory_id == source_id,
        MemoryRelation.target_memory_id == target_id,
        MemoryRelation.relation_type == rel_type,
    )
    return session.exec(stmt).first()


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def list_relations(session: Session, memory_id: UUID) -> list[MemoryRelation]:
    """List all relations where this memory is source OR target."""
    stmt = select(MemoryRelation).where(
        or_(
            MemoryRelation.source_memory_id == memory_id,
            MemoryRelation.target_memory_id == memory_id,
        )
    )
    return list(session.exec(stmt).all())


def expand_relations_for_memories(
    session: Session, memory_ids: list[UUID],
) -> dict[UUID, list[MemoryRelation]]:
    """Batch-load outgoing relations for multiple memories."""
    if not memory_ids:
        return {}
    stmt = select(MemoryRelation).where(
        MemoryRelation.source_memory_id.in_(memory_ids)
    )
    relations = list(session.exec(stmt).all())
    result: dict[UUID, list[MemoryRelation]] = {}
    for rel in relations:
        result.setdefault(rel.source_memory_id, []).append(rel)
    return result


def list_contradictions(
    session: Session,
    namespace_id: UUID | None = None,
    page: int = 1,
    size: int = 20,
    namespace_ids: list[UUID] | None = None,
) -> tuple[list[MemoryRelation], int]:
    """List CONTRADICTS relations, excluding pairs where either end is DELETED.

    Optionally filtered by namespace(s) on the source memory.
    """
    from sqlmodel import func
    from sqlalchemy.orm import aliased

    src_mem = aliased(Memory)
    tgt_mem = aliased(Memory)

    def _apply_filters(stmt):
        stmt = (
            stmt.join(src_mem, MemoryRelation.source_memory_id == src_mem.id)
                .join(tgt_mem, MemoryRelation.target_memory_id == tgt_mem.id)
                .where(MemoryRelation.relation_type == RelationType.CONTRADICTS)
                .where(src_mem.status != MemoryStatus.DELETED)
                .where(tgt_mem.status != MemoryStatus.DELETED)
        )
        if namespace_id:
            stmt = stmt.where(src_mem.namespace_id == namespace_id)
        elif namespace_ids:
            stmt = stmt.where(src_mem.namespace_id.in_(namespace_ids))
        return stmt

    count_stmt = _apply_filters(select(func.count()).select_from(MemoryRelation))
    total = session.exec(count_stmt).one()

    page_stmt = _apply_filters(select(MemoryRelation)).order_by(
        MemoryRelation.created_at.desc()
    )
    paginated = list(session.exec(page_stmt.offset((page - 1) * size).limit(size)).all())
    return paginated, total


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def delete_relation(session: Session, relation_id: UUID) -> bool:
    rel = session.get(MemoryRelation, relation_id)
    if not rel:
        return False
    session.delete(rel)
    session.commit()
    return True


# ---------------------------------------------------------------------------
# Contradiction resolution
# ---------------------------------------------------------------------------

def resolve_contradiction(
    session: Session,
    relation_id: UUID,
    action: str,
    reason: str,
    operator_id: UUID | None = None,
) -> tuple[bool, str]:
    """Resolve a CONTRADICTS relation. Returns (success, detail_message)."""
    rel = session.get(MemoryRelation, relation_id)
    if not rel:
        return False, "关系不存在"
    if rel.relation_type != RelationType.CONTRADICTS:
        return False, "该关系不是 CONTRADICTS 类型"

    if action == "keep_source":
        detail = _resolve_keep_source(session, rel, reason, operator_id)
    elif action == "keep_target":
        detail = _resolve_keep_target(session, rel, reason, operator_id)
    elif action == "keep_both":
        detail = _resolve_keep_both(session, rel)
    else:
        return False, f"未知操作: {action}"

    session.delete(rel)
    session.commit()
    return True, detail


def _resolve_keep_source(
    session: Session, rel: MemoryRelation, reason: str, operator_id: UUID | None,
) -> str:
    """Keep source (new), soft-delete target (old), create SUPERSEDES."""
    _soft_delete_memory(session, rel.target_memory_id, reason, operator_id)
    _create_supersedes_edge(session, rel.source_memory_id, rel.target_memory_id)
    _clear_pending_flag(session, rel.source_memory_id)
    return f"保留新记忆 {rel.source_memory_id}，淘汰旧记忆 {rel.target_memory_id}"


def _resolve_keep_target(
    session: Session, rel: MemoryRelation, reason: str, operator_id: UUID | None,
) -> str:
    """Keep target (old), soft-delete source (new), create SUPERSEDES."""
    _soft_delete_memory(session, rel.source_memory_id, reason, operator_id)
    _create_supersedes_edge(session, rel.target_memory_id, rel.source_memory_id)
    _clear_pending_flag(session, rel.target_memory_id)
    return f"保留旧记忆 {rel.target_memory_id}，淘汰新记忆 {rel.source_memory_id}"


def _resolve_keep_both(session: Session, rel: MemoryRelation) -> str:
    """Keep both memories, just clear pending flags."""
    _clear_pending_flag(session, rel.source_memory_id)
    _clear_pending_flag(session, rel.target_memory_id)
    return f"保留两条记忆 {rel.source_memory_id} 和 {rel.target_memory_id}"


def _soft_delete_memory(
    session: Session, memory_id: UUID, reason: str, operator_id: UUID | None,
) -> None:
    """Soft-delete a memory, remove from ES, and log the operation."""
    mem = session.get(Memory, memory_id)
    if not mem:
        return
    from forum_memory.services.memory_service import _resolve_es_index
    from forum_memory.services import es_service

    index_name = _resolve_es_index(session, mem.namespace_id)
    mem.status = MemoryStatus.DELETED
    mem.indexed_at = None
    _log_resolution(session, mem, reason, operator_id)
    # Flush DB state, then remove from ES (best-effort)
    session.flush()
    try:
        es_service.delete_memory_doc(memory_id, index_name=index_name)
    except Exception:
        logger.warning("ES delete failed for memory %s during contradiction resolve", memory_id)


def _create_supersedes_edge(session: Session, winner_id: UUID, loser_id: UUID) -> None:
    """Create a SUPERSEDES edge from winner to loser (idempotent)."""
    existing = _find_existing(session, winner_id, loser_id, RelationType.SUPERSEDES)
    if existing:
        return
    edge = MemoryRelation(
        source_memory_id=winner_id,
        target_memory_id=loser_id,
        relation_type=RelationType.SUPERSEDES,
        origin="admin_resolve",
    )
    session.add(edge)


def _clear_pending_flag(session: Session, memory_id: UUID) -> None:
    """Clear pending_human_confirm flag."""
    mem = session.get(Memory, memory_id)
    if mem and mem.pending_human_confirm:
        mem.pending_human_confirm = False


def _log_resolution(
    session: Session, memory: Memory, reason: str, operator_id: UUID | None,
) -> None:
    """Add an operation log entry for the contradiction resolution."""
    from forum_memory.models.operation_log import OperationLog

    log_entry = OperationLog(
        memory_id=memory.id,
        operation=OperationType.DELETE,
        operator_id=operator_id,
        operator_type="admin",
        reason=f"contradiction_resolve: {reason}",
        before_snapshot={"content": memory.content, "status": memory.status},
    )
    session.add(log_entry)
