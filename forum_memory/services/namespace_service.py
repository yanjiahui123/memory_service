"""Namespace (board) service — sync."""

import re
import logging
import uuid as _uuid
from uuid import UUID

from sqlmodel import Session, select, func, or_

from forum_memory.models.namespace import Namespace
from forum_memory.models.namespace_moderator import NamespaceModerator
from forum_memory.models.thread import Thread
from forum_memory.models.memory import Memory
from forum_memory.models.user import User
from forum_memory.models.enums import (
    ThreadStatus, Authority, ResolvedType, MemoryStatus, AccessMode, SystemRole,
)
from forum_memory.schemas.namespace import NamespaceCreate, NamespaceUpdate, NamespaceStats
from forum_memory.config import get_settings

logger = logging.getLogger(__name__)


def slugify(text: str) -> str:
    """Convert arbitrary text to an ES-safe slug (lowercase, no spaces/special chars).

    Rules: lowercase, replace non-alphanumeric (except -) with _, collapse
    consecutive underscores, strip leading/trailing _ or -.
    """
    s = text.lower()
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff\-]", "_", s)      # keep letters, digits, CJK, hyphen
    s = re.sub(r"[_]+", "_", s)                             # collapse underscores
    s = s.strip("_-")
    return s or "board"


def generate_namespace_name(display_name: str) -> str:
    """Auto-generate a unique, ES-safe internal name: {slug}_{8-hex}."""
    slug = slugify(display_name)
    short_id = _uuid.uuid4().hex[:8]
    return f"{slug}_{short_id}"


def list_namespaces(session: Session, user: User | None = None) -> list[Namespace]:
    """Return active namespaces visible to the user.

    PRIVATE boards are hidden from non-members. SUPER_ADMIN sees all.
    """
    stmt = select(Namespace).where(Namespace.is_active.is_(True))
    if user and user.role != SystemRole.SUPER_ADMIN:
        member_ns = select(NamespaceModerator.namespace_id).where(
            NamespaceModerator.user_id == user.id
        )
        stmt = stmt.where(
            or_(
                Namespace.access_mode != AccessMode.PRIVATE,
                Namespace.owner_id == user.id,
                Namespace.id.in_(member_ns),
            )
        )
    return list(session.exec(stmt).all())


def get_namespace(session: Session, ns_id: UUID) -> Namespace | None:
    return session.get(Namespace, ns_id)


def _generate_es_index_name() -> str:
    """Generate ES-safe index name using UUID (always lowercase ASCII)."""
    settings = get_settings()
    short_id = _uuid.uuid4().hex[:12]
    return f"{settings.es_index_prefix}_{short_id}"


def create_namespace(
    session: Session,
    data: NamespaceCreate,
    owner_id: UUID,
    add_as_moderator: bool = False,
) -> Namespace:
    name = generate_namespace_name(data.display_name)
    index_name = _generate_es_index_name()
    ns = Namespace(
        name=name,
        display_name=data.display_name,
        description=data.description,
        access_mode=data.access_mode,
        owner_id=owner_id,
        es_index_name=index_name,
    )
    session.add(ns)
    session.commit()
    session.refresh(ns)

    # 板块管理员创建板块时，自动将其加入 namespace_moderators
    if add_as_moderator:
        from forum_memory.models.namespace_moderator import NamespaceModerator
        mod = NamespaceModerator(user_id=owner_id, namespace_id=ns.id)
        session.add(mod)
        session.commit()

    # Create the ES index for this namespace
    try:
        from forum_memory.services.es_service import ensure_index_by_name
        ensure_index_by_name(index_name)
    except Exception:
        logger.warning("Failed to create ES index %s (non-fatal)", index_name)
    return ns


def update_namespace(session: Session, ns_id: UUID, data: NamespaceUpdate) -> Namespace | None:
    ns = session.get(Namespace, ns_id)
    if not ns:
        return None
    update_dict = data.model_dump(exclude_unset=True)
    for key, val in update_dict.items():
        setattr(ns, key, val)
    session.commit()
    session.refresh(ns)
    return ns


def delete_namespace(session: Session, ns_id: UUID) -> Namespace:
    """Soft-delete a namespace and cascade to threads, memories, and ES index."""
    from forum_memory.models.event import DomainEvent
    from forum_memory.services import es_service

    ns = session.get(Namespace, ns_id)
    if not ns:
        raise ValueError("Namespace not found")

    # 1. Soft-delete all non-deleted threads in this namespace
    thread_stmt = select(Thread).where(
        Thread.namespace_id == ns_id,
        Thread.status != ThreadStatus.DELETED,
    )
    threads = list(session.exec(thread_stmt).all())
    for t in threads:
        t.status = ThreadStatus.DELETED
    logger.info("Soft-deleted %d threads for namespace %s", len(threads), ns_id)

    # 2. Soft-delete all non-deleted memories in this namespace
    mem_stmt = select(Memory).where(
        Memory.namespace_id == ns_id,
        Memory.status != MemoryStatus.DELETED,
    )
    memories = list(session.exec(mem_stmt).all())
    for m in memories:
        m.status = MemoryStatus.DELETED
    logger.info("Soft-deleted %d memories for namespace %s", len(memories), ns_id)

    # 3. Mark all pending domain events for this namespace as processed
    event_stmt = select(DomainEvent).where(
        DomainEvent.namespace_id == ns_id,
        DomainEvent.processed.is_(False),
    )
    events = list(session.exec(event_stmt).all())
    for e in events:
        e.processed = True
    logger.info("Marked %d pending events as processed for namespace %s", len(events), ns_id)

    # 4. Mark namespace as inactive
    ns.is_active = False
    session.commit()
    session.refresh(ns)

    # 5. Delete ES index (non-fatal, after DB commit)
    if ns.es_index_name:
        try:
            es_service.delete_index(ns.es_index_name)
        except Exception:
            logger.warning("Failed to delete ES index %s (non-fatal)", ns.es_index_name)

    return ns


def update_dictionary(session: Session, ns_id: UUID, entries: dict) -> Namespace | None:
    ns = session.get(Namespace, ns_id)
    if not ns:
        return None
    merged = {**ns.dictionary, **entries}
    ns.dictionary = merged
    session.commit()
    session.refresh(ns)
    return ns


def get_stats(session: Session, ns_id: UUID) -> NamespaceStats:
    """Compute board-level stats."""
    total = _count_threads(session, ns_id, None)
    open_t = _count_threads(session, ns_id, ThreadStatus.OPEN)
    resolved = _count_threads(session, ns_id, ThreadStatus.RESOLVED)
    total_mem = _count_memories(session, ns_id, None)
    locked = _count_memories(session, ns_id, Authority.LOCKED)
    ai_rate = _ai_resolve_rate(session, ns_id)
    return NamespaceStats(
        total_threads=total,
        open_threads=open_t,
        resolved_threads=resolved,
        total_memories=total_mem,
        locked_memories=locked,
        ai_resolve_rate=ai_rate,
    )


def _count_threads(session: Session, ns_id: UUID, status: ThreadStatus | None) -> int:
    stmt = (
        select(func.count()).select_from(Thread)
        .where(Thread.namespace_id == ns_id)
        .where(Thread.status != ThreadStatus.DELETED)
    )
    if status:
        stmt = stmt.where(Thread.status == status)
    return session.exec(stmt).one()


def _count_memories(session: Session, ns_id: UUID, authority: Authority | None) -> int:
    stmt = (
        select(func.count()).select_from(Memory)
        .where(Memory.namespace_id == ns_id)
        .where(Memory.status != MemoryStatus.DELETED)
    )
    if authority:
        stmt = stmt.where(Memory.authority == authority)
    return session.exec(stmt).one()


def get_aggregate_stats(session: Session) -> NamespaceStats:
    """Compute aggregate stats across all active namespaces."""
    not_deleted = Thread.status != ThreadStatus.DELETED
    total_threads = session.exec(
        select(func.count()).select_from(Thread).where(not_deleted)
    ).one()
    open_threads = session.exec(
        select(func.count()).select_from(Thread)
        .where(not_deleted, Thread.status == ThreadStatus.OPEN)
    ).one()
    resolved_threads = session.exec(
        select(func.count()).select_from(Thread)
        .where(not_deleted, Thread.status == ThreadStatus.RESOLVED)
    ).one()
    total_memories = session.exec(
        select(func.count()).select_from(Memory).where(Memory.status != MemoryStatus.DELETED)
    ).one()
    locked_memories = session.exec(
        select(func.count()).select_from(Memory)
        .where(Memory.status != MemoryStatus.DELETED)
        .where(Memory.authority == Authority.LOCKED)
    ).one()

    # AI resolve rate based on organic (non-imported) threads only
    organic_resolved = session.exec(
        select(func.count()).select_from(Thread)
        .where(not_deleted, Thread.status == ThreadStatus.RESOLVED, Thread.is_imported.is_(False))
    ).one()
    ai_rate = 0.0
    if organic_resolved > 0:
        ai_count = session.exec(
            select(func.count()).select_from(Thread)
            .where(not_deleted, Thread.is_imported.is_(False), Thread.resolved_type == ResolvedType.AI_RESOLVED)
        ).one()
        ai_rate = round(ai_count / organic_resolved, 4)

    pending_count = session.exec(
        select(func.count()).select_from(Memory)
        .where(Memory.status != MemoryStatus.DELETED)
        .where(Memory.pending_human_confirm.is_(True))
    ).one()

    return NamespaceStats(
        total_threads=total_threads,
        open_threads=open_threads,
        resolved_threads=resolved_threads,
        total_memories=total_memories,
        locked_memories=locked_memories,
        ai_resolve_rate=ai_rate,
        pending_count=pending_count,
    )


def _ai_resolve_rate(session: Session, ns_id: UUID) -> float:
    """AI resolve rate based on organic (non-imported) threads only."""
    organic_resolved = session.exec(
        select(func.count()).select_from(Thread)
        .where(
            Thread.namespace_id == ns_id,
            Thread.status == ThreadStatus.RESOLVED,
            Thread.is_imported.is_(False),
        )
    ).one()
    if organic_resolved == 0:
        return 0.0
    ai_count = session.exec(
        select(func.count()).select_from(Thread)
        .where(
            Thread.namespace_id == ns_id,
            Thread.status != ThreadStatus.DELETED,
            Thread.is_imported.is_(False),
            Thread.resolved_type == ResolvedType.AI_RESOLVED,
        )
    ).one()
    return round(ai_count / organic_resolved, 4)
