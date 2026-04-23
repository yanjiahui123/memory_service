"""Thread and comment service — sync."""

import logging
from uuid import UUID
from datetime import datetime, timezone, timedelta

from sqlalchemy import cast, Text, or_, update as sa_update
from sqlmodel import Session, select

from forum_memory.models.thread import Thread, Comment
from forum_memory.models.event import DomainEvent
from forum_memory.models.namespace import Namespace
from forum_memory.models.enums import ThreadStatus, ResolvedType
from forum_memory.core.state_machine import can_transition
from forum_memory.schemas.thread import ThreadCreate, CommentCreate
from forum_memory.schemas.memory import MemorySearchRequest
from forum_memory.core.prompts import AI_ANSWER_SYSTEM_V2, AI_ANSWER_USER_V2
from forum_memory.core.image_preprocessor import (
    enrich_with_image_descriptions, has_images, strip_image_markdown,
)

logger = logging.getLogger(__name__)


def _build_sort_clause(sort: str | None):
    """Map sort key to SQLAlchemy ORDER BY clause."""
    if sort == "active":
        return Thread.updated_at.desc()
    if sort == "views":
        return Thread.view_count.desc()
    if sort == "unanswered":
        return Thread.comment_count.asc()
    # default: newest
    return Thread.created_at.desc()


def list_threads(
    session: Session,
    namespace_id: UUID | None = None,
    status: str | None = None,
    page: int = 1,
    size: int = 20,
    q: str | None = None,
    author_id: UUID | None = None,
    priority: str | None = None,
    sort: str | None = None,
) -> list[Thread]:
    stmt = (
        select(Thread)
        .join(Namespace, Thread.namespace_id == Namespace.id)
        .where(Thread.status != ThreadStatus.DELETED)
        .where(Namespace.is_active.is_(True))
        .order_by(_build_sort_clause(sort))
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
    """Atomic +1 on view_count, no select needed."""
    session.execute(
        sa_update(Thread).where(Thread.id == thread_id).values(view_count=Thread.view_count + 1)
    )
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

    # Notify namespace moderators about the new thread
    from forum_memory.services.notification_service import notify_admins_on_new_thread
    notify_admins_on_new_thread(session, thread)
    session.commit()

    # AI 回答由前端 SSE 流式端点驱动（ThreadDetail 页面自动连接），
    # 此处不再提交后台任务，避免与 SSE 竞态导致双重 LLM 调用。
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


def close_thread(session: Session, thread_id: UUID) -> Thread:
    """Manually close a thread.

    If the thread has an adopted best answer, it is marked as RESOLVED
    (with the appropriate AI/HUMAN resolved type).  Otherwise it is
    marked as CLOSED with MANUAL_CLOSED.
    """
    thread = session.get(Thread, thread_id)
    if not thread:
        raise ValueError("Thread not found")

    has_best = thread.best_answer_id is not None
    target_status = ThreadStatus.RESOLVED if has_best else ThreadStatus.CLOSED
    if not can_transition(thread.status, target_status):
        raise ValueError(f"Cannot close thread in {thread.status} state")

    if has_best:
        resolved_type = _determine_resolved_type(session, thread.best_answer_id)
        event_type = "thread.resolved"
        _mark_best_answer(session, thread.best_answer_id)
    else:
        resolved_type = ResolvedType.MANUAL_CLOSED
        event_type = "thread.closed"

    thread.status = target_status
    thread.resolved_type = resolved_type
    thread.resolved_at = datetime.now(tz=timezone(timedelta(hours=8)))
    _add_event(session, event_type, "Thread", thread,
               {"resolved_type": resolved_type.value})
    session.commit()
    session.refresh(thread)

    if has_best:
        _update_resolved_citations(session, thread_id)

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
        es_service.bulk_delete_memory_docs([(m.id, index_name) for m in memories])

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
    if thread.status == ThreadStatus.DELETED:
        raise ValueError("Cannot comment on a deleted thread")

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
    _touch_thread_updated_at(session, data.thread_id)

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


def toggle_upvote(session: Session, comment_id: UUID, user_id: UUID, thread_id: UUID | None = None) -> tuple[Comment, bool]:
    """Toggle upvote on a comment. Returns (comment, voted)."""
    from forum_memory.models.vote import CommentVote
    comment = session.get(Comment, comment_id)
    if not comment:
        raise ValueError("Comment not found")
    if thread_id and comment.thread_id != thread_id:
        raise ValueError("Comment does not belong to this thread")

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


def delete_comment(session: Session, comment_id: UUID, user_id: UUID, thread_id: UUID | None = None, is_board_admin: bool = False) -> Thread:
    """Soft-delete a comment. Only comment author or board admin can delete.
    Returns the parent thread.
    """
    comment = session.get(Comment, comment_id)
    if not comment:
        raise ValueError("Comment not found")
    if thread_id and comment.thread_id != thread_id:
        raise ValueError("Comment does not belong to this thread")
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


def _query_rag_context(
    ns_config: dict, question: str, enabled: bool, uid: str = "forum_memory",
) -> tuple[str, str | None]:
    """Query RAG knowledge base. Returns (rag_prompt, rag_chunks_json)."""
    if not enabled:
        return "(knowledge base search disabled)", None
    from forum_memory.services.rag_service import query_rag
    kb_sn_list = ns_config.get("kb_sn_list", [])
    if not kb_sn_list:
        return "(no knowledge base configured)", None
    top_k = min(max(int(ns_config.get("rag_top_k", 5)), 1), 10)
    rag_prompt_text, rag_chunks_json = query_rag(kb_sn_list, question, uid=uid, top_k=top_k)
    if rag_prompt_text:
        return rag_prompt_text, rag_chunks_json
    return "(no knowledge base configured)", None


def _build_search_and_llm_questions(title: str, content: str) -> tuple[str, str]:
    """Build two versions of the question from thread title + content.

    Returns (search_query, llm_question):
      - search_query:  title + clean text + image keywords (concise, for recall)
      - llm_question:  title + enriched content with full image descriptions
    """
    if not has_images(content):
        combined = f"{title}\n{content}"
        return combined, combined

    from forum_memory.providers import get_provider
    result = enrich_with_image_descriptions(content, get_provider())
    llm_question = f"{title}\n{result.enriched_text}"
    clean_text = strip_image_markdown(content)
    search_parts = [title, clean_text]
    if result.search_terms:
        search_parts.append(result.search_terms)
    search_query = "\n".join(search_parts)
    return search_query, llm_question


def _prepare_ai_context(session: Session, thread_id: UUID) -> tuple[list[dict], list[UUID], str | None]:
    """Pre-process: image enrich + search memories + RAG.

    Returns (messages, cited_ids, rag_context).
    Uses concise search_query for recall, rich llm_question for LLM prompt.
    """
    thread = session.get(Thread, thread_id)
    if not thread:
        raise ValueError("Thread not found")

    search_query, llm_question = _build_search_and_llm_questions(thread.title, thread.content)
    namespace = session.get(Namespace, thread.namespace_id)
    ns_config = (namespace.config or {}) if namespace else {}

    # 使用板块 owner 的 employee_id 调用 RAG，确保有知识库访问权限
    owner_id = namespace.owner_id if namespace else None
    rag_uid = _get_employee_id(session, owner_id)

    memories_text, cited_ids = _search_related_memories(
        session, search_query, thread.namespace_id, ns_config.get("enable_memory_search", True),
    )
    rag_context_prompt, stored_rag_context = _query_rag_context(
        ns_config, search_query, ns_config.get("enable_rag_search", True), uid=rag_uid,
    )

    messages = [
        {"role": "system", "content": AI_ANSWER_SYSTEM_V2.format(memories=memories_text)},
        {"role": "user", "content": AI_ANSWER_USER_V2.format(
            question=llm_question, rag_context=rag_context_prompt,
        )},
    ]
    return messages, cited_ids, stored_rag_context


def generate_ai_answer(session: Session, thread_id: UUID) -> Comment | None:
    """Search memories, query RAG if configured, and generate an AI answer for a thread."""
    from forum_memory.providers import get_provider

    messages, cited_ids, stored_rag_context = _prepare_ai_context(session, thread_id)
    answer = get_provider().complete(messages)

    if not answer or not answer.strip():
        logger.warning("LLM returned empty answer for thread %s, skipping upsert", thread_id)
        return None

    comment = _upsert_ai_comment(session, thread_id, answer, cited_ids, stored_rag_context)
    session.commit()
    session.refresh(comment)
    return comment


_AI_PLACEHOLDER = "<!-- ai_generating -->"

# ---------------------------------------------------------------------------
# TokenBuffer — shared in-memory buffer for SSE "resume" on page refresh
# ---------------------------------------------------------------------------
import threading as _threading
import queue as _queue

_SENTINEL_DONE = "__DONE__"
_SENTINEL_ERROR = "__ERROR__"
_BUFFER_TTL = 60  # seconds to keep buffer after completion


class _TokenBuffer:
    """Thread-safe token buffer for a single thread's AI generation.

    - Writer (LLM bg thread): appends tokens via `put()`, marks done via `finish()`.
    - Reader (SSE generator): iterates from any offset via `iter_from(offset)`.
    """

    def __init__(self):
        self.tokens: list[str] = []
        self.done = False
        self.error: str | None = None
        self.lock = _threading.Lock()
        self.event = _threading.Event()  # signaled on each new token / done

    def put(self, token: str) -> None:
        with self.lock:
            self.tokens.append(token)
        self.event.set()

    def finish(self, error: str | None = None) -> None:
        with self.lock:
            self.done = True
            self.error = error
        self.event.set()

    def iter_from(self, offset: int = 0):
        """Yield tokens starting from *offset*, blocking until done."""
        idx = offset
        while True:
            self.event.wait(timeout=300)
            with self.lock:
                snapshot = self.tokens[idx:]
                is_done = self.done
                err = self.error
            for tok in snapshot:
                yield tok
            idx += len(snapshot)
            if is_done:
                if err:
                    raise RuntimeError(err)
                return
            self.event.clear()


class _BufferManager:
    """Global registry of active TokenBuffers, keyed by thread_id."""

    def __init__(self):
        self._buffers: dict[UUID, _TokenBuffer] = {}
        self._lock = _threading.Lock()

    def create(self, thread_id: UUID) -> _TokenBuffer:
        buf = _TokenBuffer()
        with self._lock:
            self._buffers[thread_id] = buf
        return buf

    def get(self, thread_id: UUID) -> _TokenBuffer | None:
        with self._lock:
            return self._buffers.get(thread_id)

    def remove(self, thread_id: UUID) -> None:
        with self._lock:
            self._buffers.pop(thread_id, None)

    def schedule_remove(self, thread_id: UUID) -> None:
        """Remove buffer after TTL so late-arriving connections still work."""
        def _delayed():
            import time
            time.sleep(_BUFFER_TTL)
            self.remove(thread_id)
        t = _threading.Thread(target=_delayed, daemon=True)
        t.start()


_buffers = _BufferManager()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_active_buffer(thread_id: UUID) -> _TokenBuffer | None:
    """Return the active token buffer if LLM is generating for this thread."""
    return _buffers.get(thread_id)


def start_ai_generation(
    session: Session,
    thread_id: UUID,
    on_complete: "callable | None" = None,
) -> _TokenBuffer:
    """Kick off LLM generation in background thread, return token buffer.

    1. Prepare context (search memories + RAG)
    2. Create placeholder AI comment
    3. Spawn bg thread that streams LLM into a shared TokenBuffer
    """
    messages, cited_ids, stored_rag_context = _prepare_ai_context(session, thread_id)

    _upsert_ai_comment(session, thread_id, _AI_PLACEHOLDER, [], None)
    session.commit()

    buf = _buffers.create(thread_id)
    bg = _threading.Thread(
        target=_llm_worker,
        args=(thread_id, messages, cited_ids, stored_rag_context, buf, on_complete),
        daemon=True,
    )
    bg.start()
    return buf


def _llm_worker(
    thread_id: UUID,
    messages: list[dict],
    cited_ids: list[UUID],
    stored_rag_context: str | None,
    buf: _TokenBuffer,
    on_complete: "callable | None",
) -> None:
    """Run LLM stream to completion in background thread, always save result."""
    from forum_memory.providers import get_provider

    parts: list[str] = []
    try:
        for chunk in get_provider().complete_stream(messages):
            parts.append(chunk)
            buf.put(chunk)
        # 先持久化到数据库，再通知 SSE done，避免前端 refetch 拿到旧内容
        full_answer = "".join(parts)
        if full_answer.strip():
            _persist_ai_answer(thread_id, full_answer, cited_ids, stored_rag_context)
        else:
            logger.warning("LLM empty answer for thread %s", thread_id)
        buf.finish()
    except Exception as exc:
        logger.exception("LLM stream failed for thread %s", thread_id)
        buf.finish(error=str(exc))
    finally:
        if on_complete:
            on_complete()
        _buffers.schedule_remove(thread_id)


def _persist_ai_answer(
    thread_id: UUID,
    content: str,
    cited_ids: list[UUID],
    stored_rag_context: str | None,
) -> None:
    """Save final AI answer using a fresh DB session (called from bg thread)."""
    from forum_memory.database import engine

    try:
        with Session(engine) as bg_session:
            _upsert_ai_comment(bg_session, thread_id, content, cited_ids, stored_rag_context)
            bg_session.commit()
        logger.info("AI answer saved (%d chars) for thread %s", len(content), thread_id)
    except Exception:
        logger.exception("Failed to save AI answer for thread %s", thread_id)


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
        old_ids = _parse_cited_ids(existing.cited_memory_ids)
        _decrement_cite_counts(session, old_ids)
        existing.content = content
        existing.cited_memory_ids = [str(mid) for mid in cited_ids]
        existing.rag_context = rag_context
        _increment_cite_counts(session, cited_ids)
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


def _parse_cited_ids(raw: list | None) -> list[UUID]:
    """Parse cited_memory_ids JSON list to UUID list, ignoring invalid entries."""
    if not raw:
        return []
    result = []
    for mid in raw:
        try:
            result.append(UUID(str(mid)))
        except (ValueError, AttributeError):
            pass
    return result


def _increment_cite_counts(session: Session, cited_ids: list[UUID]) -> None:
    """Increment cite_count for cited memories."""
    if not cited_ids:
        return
    from forum_memory.models.memory import Memory
    session.execute(
        sa_update(Memory).where(Memory.id.in_(cited_ids)).values(cite_count=Memory.cite_count + 1)
    )


def _decrement_cite_counts(session: Session, cited_ids: list[UUID]) -> None:
    """Decrement cite_count for previously cited memories (floor at 0)."""
    if not cited_ids:
        return
    from forum_memory.models.memory import Memory
    from sqlalchemy import case
    session.execute(
        sa_update(Memory).where(Memory.id.in_(cited_ids)).values(
            cite_count=case((Memory.cite_count > 0, Memory.cite_count - 1), else_=0)
        )
    )


def batch_timeout_threads(session: Session, timeout_days: int = 7) -> int:
    """Batch timeout-close OPEN threads older than timeout_days. Returns count closed."""
    now = datetime.now(tz=timezone(timedelta(hours=8)))
    cutoff = now - timedelta(days=timeout_days)
    threads = list(session.exec(
        select(Thread)
        .where(Thread.status == ThreadStatus.OPEN)
        .where(Thread.created_at < cutoff)
    ).all())
    if not threads:
        return 0
    thread_ids = [t.id for t in threads]
    session.execute(
        sa_update(Thread)
        .where(Thread.id.in_(thread_ids))
        .values(
            status=ThreadStatus.TIMEOUT_CLOSED,
            resolved_type=ResolvedType.TIMEOUT,
            timeout_at=now,
        )
    )
    events = [
        DomainEvent(
            event_type="thread.timeout_closed",
            aggregate_type="Thread",
            aggregate_id=t.id,
            namespace_id=t.namespace_id,
            payload={},
        )
        for t in threads
    ]
    session.add_all(events)
    session.commit()
    logger.info("Batch timeout-closed %d threads", len(threads))
    return len(threads)


def reconcile_comment_counts(session: Session) -> int:
    """Fix drifted comment_count by reconciling against actual Comment rows.
    Returns the number of threads corrected.
    """
    from sqlalchemy import text as sa_text

    drift_sql = (
        "SELECT t.id, t.comment_count, COALESCE(c.cnt, 0) AS actual "
        "FROM memo_threads t "
        "LEFT JOIN (SELECT thread_id, COUNT(*) AS cnt FROM memo_comments "
        "           WHERE deleted_at IS NULL GROUP BY thread_id) c "
        "  ON t.id = c.thread_id "
        "WHERE t.status != 'DELETED' AND t.comment_count != COALESCE(c.cnt, 0)"
    )
    rows = session.execute(sa_text(drift_sql)).all()
    if not rows:
        return 0
    for row in rows:
        logger.info(
            "comment_count drift: thread %s had %d, actual %d",
            row[0], row[1], row[2],
        )
    session.execute(sa_text(
        "UPDATE memo_threads t "
        f"SET comment_count = sub.actual FROM ({drift_sql}) sub "
        "WHERE t.id = sub.id"
    ))
    session.commit()
    logger.info("Reconciled comment_count for %d threads", len(rows))
    return len(rows)


def _increment_comment_count(session: Session, thread_id: UUID) -> None:
    thread = session.get(Thread, thread_id)
    if thread:
        thread.comment_count += 1


def _touch_thread_updated_at(session: Session, thread_id: UUID) -> None:
    """Explicitly refresh thread.updated_at so the list page shows latest activity time."""
    thread = session.get(Thread, thread_id)
    if thread:
        thread.updated_at = datetime.now(tz=timezone(timedelta(hours=8)))


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

    from forum_memory.services.memory_service import refresh_quality_batch
    try:
        refresh_quality_batch(session, cited_ids)
    except Exception:
        logger.warning("Failed to refresh quality for resolved thread %s", thread_id, exc_info=True)


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
