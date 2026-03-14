"""Memory relation CRUD service — sync."""

import logging
from uuid import UUID

from sqlmodel import Session, select, or_

from forum_memory.models.memory_relation import MemoryRelation
from forum_memory.models.memory import Memory
from forum_memory.models.enums import RelationType, MemoryStatus

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
    for mid in (id_a, id_b):
        mem = session.get(Memory, mid)
        if not mem:
            return False
    return True


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
) -> tuple[list[MemoryRelation], int]:
    """List CONTRADICTS relations, optionally filtered by namespace."""
    stmt = select(MemoryRelation).where(
        MemoryRelation.relation_type == RelationType.CONTRADICTS
    )
    if namespace_id:
        stmt = stmt.join(
            Memory, MemoryRelation.source_memory_id == Memory.id
        ).where(Memory.namespace_id == namespace_id)
    all_items = list(session.exec(stmt).all())
    total = len(all_items)
    paginated = list(session.exec(stmt.offset((page - 1) * size).limit(size)).all())
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
