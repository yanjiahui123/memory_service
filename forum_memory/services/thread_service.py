"""Thread and comment service — sync."""

import logging
from uuid import UUID
from datetime import datetime, timezone, timedelta

from sqlalchemy import cast, Text, or_
from sqlmodel import Session, select

from forum_memory.models.thread import Thread, Comment
from forum_memory.models.event import DomainEvent
from forum_memory.models.namespace import Namespace
from forum_memory.models.enums import ThreadStatus, ResolvedType
from forum_memory.core.state_machine import can_transition
from forum_memory.schemas.thread import ThreadCreate, CommentCreate
from forum_memory.schemas.memory import MemorySearchRequest
from forum_memory.core.prompts import AI_ANSWER_SYSTEM, AI_ANSWER_USER

logger = logging.getLogger(__name__)


def list_threads(
    session: Session,
    namespace_id: UUID | None = None,
    status: str | None = None,
    page: int = 1,
    size: int = 20,
    q: str | None = None,
    author_id: UUID | None = None,
    priority: str | None = None,
) -> list[Thread]:
    stmt = (
        select(Thread)
        .join(Namespace, Thread.namespace_id == Namespace.id)
        .where(Thread.status != ThreadStatus.DELETED)
        .where(Namespace.is_active.is_(True))
        .order_by(Thread.created_at.desc())
    )
    if namespace_id:
        stmt = stmt.where(Thread.namespace_id == namespace_id)
    if status:
        stmt = stmt.where(Thread.status == status)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(or_(
            Thread.title.ilike(pattern),
            Thread.content.ilike(pattern),
            Thread.environment.ilike(pattern),
            cast(Thread.tags, Text).ilike(pattern),
        ))
    if author_id:
        stmt = stmt.where(Thread.author_id == author_id)
    if priority:
        stmt = stmt.where(Thread.priority == priority)
    stmt = stmt.offset((page - 1) * size).limit(size)
    return list(session.exec(stmt).all())


def count_threads(
    session: Session,
    namespace_id: UUID | None = None,
    status: str | None = None,
    q: str | None = None,
    author_id: UUID | None = None,
    priority: str | None = None,
) -> int:
    """Count threads matching the given filters (for pagination)."""
    from sqlmodel import func
    stmt = (
        select(func.count())
        .select_from(Thread)
        .join(Namespace, Thread.namespace_id == Namespace.id)
        .where(Thread.status != ThreadStatus.DELETED)
        .where(Namespace.is_active.is_(True))
    )
    if namespace_id:
        stmt = stmt.where(Thread.namespace_id == namespace_id)
    if status:
        stmt = stmt.where(Thread.status == status)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(or_(
            Thread.title.ilike(pattern),
            Thread.content.ilike(pattern),
            Thread.environment.ilike(pattern),
            cast(Thread.tags, Text).ilike(pattern),
        ))
    if author_id:
        stmt = stmt.where(Thread.author_id == author_id)
    if priority:
        stmt = stmt.where(Thread.priority == priority)
    return session.exec(stmt).one()


def get_thread(session: Session, thread_id: UUID) -> Thread | None:
    return session.get(Thread, thread_id)


def increment_view_count(session: Session, thread_id: UUID) -> None:
    thread = session.get(Thread, thread_id)
    if thread:
        thread.view_count += 1
        session.add(thread)
        session.commit()


def create_thread(session: Session, data: ThreadCreate, author_id: UUID) -> Thread:
    thread = Thread(
        namespace_id=data.namespace_id,
        author_id=author_id,
        title=data.title,
        content=data.content,
        tags=data.tags,
        knowledge_type=data.knowledge_type,
        environment=data.environment,
        priority=data.priority,
    )
    session.add(thread)
    session.commit()
    session.refresh(thread)

    # AI 回答提交到后台线程，不阻塞 HTTP 请求返回
    submit_ai_answer(thread.id)

    return thread


def submit_ai_answer(thread_id: UUID) -> None:
    """Submit AI answer generation to background thread pool.

    Uses its own DB session since the request session will be closed
    by the time the background task executes.
    """
    from forum_memory.core.background import submit

    def _task():
        from forum_memory.database import engine
        with Session(engine) as bg_session:
            try:
                generate_ai_answer(bg_session, thread_id)
                logger.info("AI answer generated (background) for thread %s", thread_id)
            except Exception:
                logger.exception(
                    "AI answer generation failed (background) for thread %s, user can retry manually",
                    thread_id,
                )

    submit(_task)


def resolve_thread(session: Session, thread_id: UUID, best_answer_id: UUID | None = None) -> Thread:
    thread = session.get(Thread, thread_id)
    if not thread:
        raise ValueError("Thread not found")
    if not can_transition(thread.status, ThreadStatus.RESOLVED):
        raise ValueError(f"Cannot resolve thread in {thread.status} state")

    # Preserve previously adopted best_answer_id if none explicitly provided
    effective_answer_id = best_answer_id if best_answer_id is not None else thread.best_answer_id
    resolved_type = _determine_resolved_type(session, effective_answer_id)
    thread.status = ThreadStatus.RESOLVED
    thread.resolved_type = resolved_type
    thread.best_answer_id = effective_answer_id
    thread.resolved_at = datetime.now(tz=timezone(timedelta(hours=8)))

    if effective_answer_id:
        _mark_best_answer(session, effective_answer_id)

    _add_event(session, "thread.resolved", "Thread", thread, {"resolved_type": resolved_type.value})
    session.commit()
    session.refresh(thread)

    # 更新被引用记忆的 resolved_citation_count，并刷新其质量分
    _update_resolved_citations(session, thread_id)

    return thread


def adopt_answer(session: Session, thread_id: UUID, best_answer_id: UUID) -> Thread:
    """Mark best answer without closing the thread (thread stays OPEN)."""
    thread = session.get(Thread, thread_id)
    if not thread:
        raise ValueError("Thread not found")
    # Clear old best_answer mark if switching to a different comment
    if thread.best_answer_id and thread.best_answer_id != best_answer_id:
        prev = session.get(Comment, thread.best_answer_id)
        if prev:
            prev.is_best_answer = False
    thread.best_answer_id = best_answer_id
    _mark_best_answer(session, best_answer_id)
    session.commit()
    session.refresh(thread)
    return thread


def reopen_thread(session: Session, thread_id: UUID) -> Thread:
    """Reopen a RESOLVED or TIMEOUT_CLOSED thread back to OPEN."""
    thread = session.get(Thread, thread_id)
    if not thread:
        raise ValueError("Thread not found")
    if not can_transition(thread.status, ThreadStatus.OPEN):
        raise ValueError(f"Cannot reopen thread in {thread.status} state")
    thread.status = ThreadStatus.OPEN
    thread.resolved_type = None
    thread.resolved_at = None
    thread.timeout_at = None
    session.commit()
    session.refresh(thread)
    return thread


def timeout_close_thread(session: Session, thread_id: UUID) -> Thread:
    thread = session.get(Thread, thread_id)
    if not thread:
        raise ValueError("Thread not found")
    if not can_transition(thread.status, ThreadStatus.TIMEOUT_CLOSED):
        raise ValueError(f"Cannot timeout-close thread in {thread.status} state")

    thread.status = ThreadStatus.TIMEOUT_CLOSED
    thread.resolved_type = ResolvedType.TIMEOUT
    thread.timeout_at = datetime.now(tz=timezone(timedelta(hours=8)))
    _add_event(session, "thread.timeout_closed", "Thread", thread)
    session.commit()
    session.refresh(thread)
    return thread


def delete_thread(session: Session, thread_id: UUID, deleted_by_admin: bool = False) -> Thread:
    """Soft-delete a thread and handle associated memories.

    - deleted_by_admin=False (author self-delete): cascade soft-delete memories + remove from ES.
    - deleted_by_admin=True (admin delete): mark memories pending_human_confirm for review.
    """
    thread = session.get(Thread, thread_id)
    if not thread:
        raise ValueError("Thread not found")
    if not can_transition(thread.status, ThreadStatus.DELETED):
        raise ValueError(f"Cannot delete thread in {thread.status} state")
    thread.status = ThreadStatus.DELETED
    _add_event(session, "thread.deleted", "Thread", thread)

    # Handle associated memories
    from forum_memory.models.memory import Memory
    from forum_memory.models.enums import MemoryStatus

    memories = list(session.exec(
        select(Memory).where(
            Memory.source_id == thread_id,
            Memory.status != MemoryStatus.DELETED,
        )
    ).all())

    if memories:
        if deleted_by_admin:
            for m in memories:
                m.pending_human_confirm = True
            logger.info(
                "Admin deleted thread %s: %d memories marked pending_human_confirm",
                thread_id, len(memories),
            )
        else:
            from forum_memory.services import es_service
            ns = session.get(Namespace, thread.namespace_id)
            index_name = ns.es_index_name if ns else None
            for m in memories:
                m.status = MemoryStatus.DELETED
                m.indexed_at = None  # Mark ES as stale for repair sensor fallback
            logger.info(
                "Author deleted thread %s: %d memories cascade-deleted",
                thread_id, len(memories),
            )

    session.commit()
    session.refresh(thread)

    # Remove from ES after successful DB commit to avoid DB/ES inconsistency
    if memories and not deleted_by_admin:
        from forum_memory.services import es_service
        ns = session.get(Namespace, thread.namespace_id)
        index_name = ns.es_index_name if ns else None
        for m in memories:
            es_service.delete_memory_doc(m.id, index_name)

    return thread


def list_comments(session: Session, thread_id: UUID) -> list[Comment]:
    stmt = (
        select(Comment)
        .where(Comment.thread_id == thread_id, Comment.deleted_at.is_(None))
        .order_by(Comment.created_at)
    )
    return list(session.exec(stmt).all())


def add_comment(session: Session, data: CommentCreate, author_id: UUID | None, is_ai: bool = False, author_role: str = "commenter") -> Comment:
    thread = session.get(Thread, data.thread_id)
    if not thread:
        raise ValueError("Thread not found")

    _validate_reply_target(session, data)

    comment = Comment(
        thread_id=data.thread_id,
        author_id=author_id,
        content=data.content,
        is_ai=is_ai,
        author_role=author_role,
        reply_to_comment_id=getattr(data, "reply_to_comment_id", None),
    )
    session.add(comment)
    _increment_comment_count(session, data.thread_id)

    # flush 使 comment 行落库，后续 Notification FK 才能引用
    if not is_ai:
        session.flush()
        from forum_memory.services.notification_service import notify_on_comment
        notify_on_comment(session, comment, thread)

    session.commit()
    session.refresh(comment)
    return comment


def _validate_reply_target(session: Session, data: CommentCreate) -> None:
    """Validate that reply_to_comment_id refers to a valid, non-deleted comment in the same thread."""
    reply_id = getattr(data, "reply_to_comment_id", None)
    if not reply_id:
        return
    parent = session.get(Comment, reply_id)
    if not parent or parent.thread_id != data.thread_id:
        raise ValueError("Reply target comment not found in this thread")
    if parent.deleted_at:
        raise ValueError("Cannot reply to a deleted comment")


def _determine_resolved_type(session: Session, best_answer_id: UUID | None) -> ResolvedType:
    if not best_answer_id:
        return ResolvedType.HUMAN_RESOLVED
    comment = session.get(Comment, best_answer_id)
    if comment and comment.is_ai:
        return ResolvedType.AI_RESOLVED
    return ResolvedType.HUMAN_RESOLVED


def _mark_best_answer(session: Session, comment_id: UUID) -> None:
    comment = session.get(Comment, comment_id)
    if comment:
        comment.is_best_answer = True


def toggle_upvote(session: Session, comment_id: UUID, user_id: UUID) -> tuple[Comment, bool]:
    """Toggle upvote on a comment. Returns (comment, voted)."""
    from forum_memory.models.vote import CommentVote
    comment = session.get(Comment, comment_id)
    if not comment:
        raise ValueError("Comment not found")

    existing = session.exec(
        select(CommentVote).where(CommentVote.comment_id == comment_id, CommentVote.user_id == user_id)
    ).first()

    if existing:
        session.delete(existing)
        comment.upvote_count = max(0, comment.upvote_count - 1)
        voted = False
    else:
        session.add(CommentVote(comment_id=comment_id, user_id=user_id))
        comment.upvote_count += 1
        voted = True

    session.commit()
    session.refresh(comment)
    return comment, voted


def delete_comment(session: Session, comment_id: UUID, user_id: UUID, is_board_admin: bool = False) -> Thread:
    """Soft-delete a comment. Only comment author or board admin can delete.
    Returns the parent thread."""
    comment = session.get(Comment, comment_id)
    if not comment:
        raise ValueError("Comment not found")
    if comment.deleted_at:
        raise ValueError("Comment already deleted")

    # Authorization: only comment author or board admin can delete
    if not is_board_admin and comment.author_id != user_id:
        raise PermissionError("Only the comment author or board admin can delete this comment")

    thread = session.get(Thread, comment.thread_id)
    if not thread:
        raise ValueError("Thread not found")
    # Clear best_answer_id if we're deleting the best answer to prevent dangling reference
    if thread.best_answer_id == comment_id:
        thread.best_answer_id = None
        logger.warning("Deleted comment %s was best_answer for thread %s — cleared reference", comment_id, thread.id)
    # Soft-delete: set deleted_at timestamp for audit trail
    comment.deleted_at = datetime.now(tz=timezone(timedelta(hours=8)))
    thread.comment_count = max(0, thread.comment_count - 1)
    session.commit()
    session.refresh(thread)
    return thread


def _search_related_memories(session, question: str, namespace_id: UUID, enabled: bool) -> tuple[str, list[UUID]]:
    """Search for memories relevant to the question. Returns (text_for_prompt, cited_ids)."""
    if not enabled:
        return "(memory search disabled)", []
    from forum_memory.services.search_service import search_memories
    search_result = search_memories(session, MemorySearchRequest(
        query=question, namespace_id=namespace_id, top_k=5,
    ))
    if not search_result.hits:
        return "(no relevant memories found)", []
    lines = []
    cited_ids = []
    for h in search_result.hits:
        lines.append(_format_hit_line(h))
        cited_ids.append(h.memory.id)
        for rel in h.related:
            lines.append(_format_relation_hint(rel))
    return "\n\n".join(lines), cited_ids


def _format_hit_line(hit) -> str:
    """Format a single search hit with authority and quality metadata."""
    mem = hit.memory
    short_id = str(mem.id)[:8]
    quality = getattr(mem, "quality_score", 0)
    return f"[M-{short_id}] ({mem.authority}, quality={quality:.2f}) {mem.content}"


def _format_relation_hint(rel) -> str:
    """Format a relation hint with type-specific markers for LLM context."""
    if rel.relation_type == "CONTRADICTS":
        conf = f", 置信度={rel.confidence:.1f}" if hasattr(rel, "confidence") else ""
        return f"  \u26a0 [存在争议{conf}] {rel.content_preview}"
    if rel.relation_type == "SUPERSEDES":
        return f"  \u26a0 [已被取代] {rel.content_preview}"
    return f"  \u21b3 [{rel.label}] {rel.content_preview}"


def _get_employee_id(session: Session, author_id: UUID | None) -> str:
    """Look up the employee_id for a user; fall back to 'forum_memory'."""
    if not author_id:
        return "forum_memory"
    from forum_memory.models.user import User
    user = session.get(User, author_id)
    if user and user.employee_id:
        return user.employee_id
    return "forum_memory"


def _query_rag_context(ns_config: dict, question: str, enabled: bool, uid: str = "forum_memory") -> tuple[str, str | None]:
    """Query RAG knowledge base. Returns (rag_prompt, rag_chunks_json)."""
    if not enabled:
        return "(knowledge base search disabled)", None
    from forum_memory.services.rag_service import query_rag
    kb_sn_list = ns_config.get("kb_sn_list", [])
    if not kb_sn_list:
        return "(no knowledge base configured)", None
    rag_prompt_text, rag_chunks_json = query_rag(kb_sn_list, question, uid=uid)
    if rag_prompt_text:
        return rag_prompt_text, rag_chunks_json
    return "(no knowledge base configured)", None


def generate_ai_answer(session: Session, thread_id: UUID) -> Comment:
    """Search memories, query RAG if configured, and generate an AI answer for a thread."""
    from forum_memory.providers import get_provider

    thread = session.get(Thread, thread_id)
    if not thread:
        raise ValueError("Thread not found")

    question = f"{thread.title}\n{thread.content}"
    namespace = session.get(Namespace, thread.namespace_id)
    ns_config = (namespace.config or {}) if namespace else {}

    author_uid = _get_employee_id(session, thread.author_id)

    memories_text, cited_ids = _search_related_memories(
        session, question, thread.namespace_id, ns_config.get("enable_memory_search", True),
    )
    rag_context_prompt, stored_rag_context = _query_rag_context(
        ns_config, question, ns_config.get("enable_rag_search", True), uid=author_uid,
    )

    answer = get_provider().complete([
        {"role": "system", "content": AI_ANSWER_SYSTEM},
        {"role": "user", "content": AI_ANSWER_USER.format(
            question=question, memories=memories_text, rag_context=rag_context_prompt,
        )},
    ])

    comment = _upsert_ai_comment(session, thread_id, answer, cited_ids, stored_rag_context)
    session.commit()
    session.refresh(comment)
    return comment


def _upsert_ai_comment(
    session: Session,
    thread_id: UUID,
    content: str,
    cited_ids: list[UUID],
    rag_context: str | None,
) -> Comment:
    """每帖只保留一条 AI 回答：有则更新内容，无则新建并计数。"""
    stmt = select(Comment).where(
        Comment.thread_id == thread_id,
        Comment.is_ai.is_(True),
        Comment.deleted_at.is_(None),
    )
    existing = session.exec(stmt).first()
    if existing:
        existing.content = content
        existing.cited_memory_ids = [str(mid) for mid in cited_ids]
        existing.rag_context = rag_context
        return existing
    comment = Comment(
        thread_id=thread_id, author_id=None, content=content,
        is_ai=True, author_role="ai",
        cited_memory_ids=[str(mid) for mid in cited_ids],
        rag_context=rag_context,
    )
    session.add(comment)
    _increment_comment_count(session, thread_id)
    _increment_cite_counts(session, cited_ids)
    return comment


def _increment_cite_counts(session: Session, cited_ids: list[UUID]) -> None:
    """Increment cite_count for cited memories."""
    if not cited_ids:
        return
    from sqlalchemy import update as sa_update
    from forum_memory.models.memory import Memory
    session.execute(
        sa_update(Memory).where(Memory.id.in_(cited_ids)).values(cite_count=Memory.cite_count + 1)
    )


def batch_timeout_threads(session: Session, timeout_days: int = 7) -> int:
    """Batch timeout-close OPEN threads older than timeout_days. Returns count closed."""
    cutoff = datetime.now(tz=timezone(timedelta(hours=8))) - timedelta(days=timeout_days)
    stmt = (
        select(Thread)
        .where(Thread.status == ThreadStatus.OPEN)
        .where(Thread.created_at < cutoff)
    )
    threads = list(session.exec(stmt).all())
    count = 0
    for t in threads:
        try:
            timeout_close_thread(session, t.id)
            count += 1
        except ValueError:
            logger.warning("Cannot timeout-close thread %s, skipping", t.id)
    logger.info("Batch timeout-closed %d threads", count)
    return count


def reconcile_comment_counts(session: Session) -> int:
    """Fix drifted comment_count by reconciling against actual Comment rows.
    Returns the number of threads corrected."""
    from sqlmodel import func
    from sqlalchemy import text as sa_text

    rows = session.execute(sa_text(
        "SELECT t.id, t.comment_count, COALESCE(c.cnt, 0) AS actual "
        "FROM memo_threads t "
        "LEFT JOIN (SELECT thread_id, COUNT(*) AS cnt FROM memo_comments "
        "           WHERE deleted_at IS NULL GROUP BY thread_id) c "
        "  ON t.id = c.thread_id "
        "WHERE t.status != 'DELETED' AND t.comment_count != COALESCE(c.cnt, 0)"
    )).all()

    for row in rows:
        thread = session.get(Thread, row[0])
        if thread:
            logger.info(
                "comment_count drift: thread %s had %d, actual %d",
                thread.id, thread.comment_count, row[2],
            )
            thread.comment_count = row[2]

    if rows:
        session.commit()
    logger.info("Reconciled comment_count for %d threads", len(rows))
    return len(rows)


def _increment_comment_count(session: Session, thread_id: UUID) -> None:
    thread = session.get(Thread, thread_id)
    if thread:
        thread.comment_count += 1


def _collect_cited_ids(ai_comments) -> set[UUID]:
    """Extract unique cited memory IDs from AI comments."""
    cited_ids: set[UUID] = set()
    for c in ai_comments:
        if not c.cited_memory_ids:
            continue
        for mid in c.cited_memory_ids:
            try:
                cited_ids.add(UUID(str(mid)))
            except (ValueError, AttributeError):
                pass
    return cited_ids


def _update_resolved_citations(session: Session, thread_id: UUID) -> None:
    """当帖子被解决时，递增所有 AI 回答所引用记忆的 resolved_citation_count，并刷新其质量分。"""
    from sqlalchemy import update as sa_update
    from forum_memory.models.memory import Memory

    ai_comments = session.exec(
        select(Comment).where(Comment.thread_id == thread_id, Comment.is_ai.is_(True))
    ).all()
    cited_ids = _collect_cited_ids(ai_comments)
    if not cited_ids:
        return

    session.execute(
        sa_update(Memory)
        .where(Memory.id.in_(cited_ids))
        .values(resolved_citation_count=Memory.resolved_citation_count + 1)
    )
    session.commit()

    from forum_memory.services.memory_service import refresh_quality
    for mid in cited_ids:
        try:
            refresh_quality(session, mid)
        except Exception:
            logger.warning("Failed to refresh quality for memory %s after resolve", mid)


def _add_event(session: Session, event_type: str, agg_type: str, thread: Thread, payload: dict | None = None) -> None:
    """Add a domain event to the current session (caller is responsible for commit)."""
    event = DomainEvent(
        event_type=event_type,
        aggregate_type=agg_type,
        aggregate_id=thread.id,
        namespace_id=thread.namespace_id,
        payload=payload or {},
    )
    session.add(event)
