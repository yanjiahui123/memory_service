"""Thread API routes — sync."""

import json
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select

from forum_memory.api.deps import get_db, get_current_user, get_current_user_id, check_board_permission, check_namespace_read_access, check_namespace_write_access
from forum_memory.api.rate_limit import limiter
from forum_memory.models.user import User
from forum_memory.models.thread import Comment
from forum_memory.models.enums import SystemRole
from forum_memory.models.namespace_moderator import NamespaceModerator
from forum_memory.schemas.thread import ThreadCreate, ThreadRead, ThreadResolve, CommentCreate, CommentRead, UpvoteResponse
from forum_memory.services import thread_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/threads", tags=["threads"])


def _enrich_threads_with_authors(session: Session, threads: list) -> list[dict]:
    """Batch-join author display names and AI answer status onto thread dicts."""
    thread_ids = [t.id for t in threads]
    author_ids = [t.author_id for t in threads if t.author_id]
    users = {}
    if author_ids:
        users = {u.id: u.display_name for u in session.exec(select(User).where(User.id.in_(author_ids))).all()}
    # Batch check which threads have AI answers
    ai_set: set = set()
    if thread_ids:
        ai_rows = session.exec(
            select(Comment.thread_id).where(Comment.thread_id.in_(thread_ids), Comment.is_ai.is_(True)).distinct()
        ).all()
        ai_set = {row for row in ai_rows}
    result = []
    for t in threads:
        d = t.model_dump()
        d["author_display_name"] = users.get(t.author_id) if t.author_id else None
        d["has_ai_answer"] = t.id in ai_set
        result.append(d)
    return result


@router.get("", response_model=list[ThreadRead])
def list_threads(
    response: Response,
    namespace_id: UUID | None = None,
    status: str | None = None,
    author_id: UUID | None = None,
    priority: str | None = None,
    q: str | None = None,
    sort: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    session: Session = Depends(get_db),
):
    items = thread_service.list_threads(session, namespace_id, status, page, size, q, author_id=author_id, priority=priority, sort=sort)
    total = thread_service.count_threads(session, namespace_id, status, q, author_id=author_id, priority=priority)
    response.headers["X-Total-Count"] = str(total)
    return _enrich_threads_with_authors(session, items)


@router.get("/{thread_id}", response_model=ThreadRead)
def get_thread(thread_id: UUID, session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    thread = thread_service.get_thread(session, thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")
    check_namespace_read_access(thread.namespace_id, session, user)
    return _enrich_threads_with_authors(session, [thread])[0]


@router.post("/{thread_id}/view", status_code=204)
def record_view(thread_id: UUID, session: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    """Increment view count (fire-and-forget, idempotency not required)."""
    thread_service.increment_view_count(session, thread_id)


@router.post("", response_model=ThreadRead, status_code=201)
@limiter.limit("10/minute")
def create_thread(request: Request, data: ThreadCreate, session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    # Check write access based on namespace access_mode
    check_namespace_write_access(data.namespace_id, session, user)
    return thread_service.create_thread(session, data, user.id)


@router.post("/{thread_id}/resolve", response_model=ThreadRead)
def resolve_thread(thread_id: UUID, data: ThreadResolve, session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    thread = thread_service.get_thread(session, thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")
    _check_thread_owner_or_admin(session, user, thread)
    try:
        return thread_service.resolve_thread(session, thread_id, data.best_answer_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/{thread_id}/close", response_model=ThreadRead)
def close_thread(thread_id: UUID, session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """主动关闭帖子（不标记为已解决）。帖子作者或管理员可操作。"""
    thread = thread_service.get_thread(session, thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")
    _check_thread_owner_or_admin(session, user, thread)
    try:
        return thread_service.close_thread(session, thread_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/{thread_id}/adopt-answer", response_model=ThreadRead)
def adopt_answer(thread_id: UUID, data: ThreadResolve, session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    thread = thread_service.get_thread(session, thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")
    _check_thread_owner_or_admin(session, user, thread)
    if not data.best_answer_id:
        raise HTTPException(400, "best_answer_id is required")
    try:
        return thread_service.adopt_answer(session, thread_id, data.best_answer_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def _check_thread_owner_or_admin(session: Session, user: User, thread) -> None:
    """只有帖子作者或板块管理员可以关闭/采纳帖子。"""
    is_author = thread.author_id == user.id
    is_admin = _is_board_admin_for_ns(session, user, thread.namespace_id)
    if not is_author and not is_admin:
        raise HTTPException(403, "只有帖子作者或管理员可以执行此操作")


def _is_board_admin_for_ns(session: Session, user: User, namespace_id: UUID) -> bool:
    if user.role == SystemRole.SUPER_ADMIN:
        return True
    if user.role == SystemRole.BOARD_ADMIN:
        stmt = select(NamespaceModerator).where(
            NamespaceModerator.user_id == user.id,
            NamespaceModerator.namespace_id == namespace_id,
        )
        return session.exec(stmt).first() is not None
    return False


@router.post("/{thread_id}/reopen", response_model=ThreadRead)
def reopen_thread(thread_id: UUID, session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    thread = thread_service.get_thread(session, thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")
    is_author = thread.author_id == user.id
    is_admin = _is_board_admin_for_ns(session, user, thread.namespace_id)
    if not is_author and not is_admin:
        raise HTTPException(403, "只有帖子作者或管理员可以重新开启帖子")
    try:
        return thread_service.reopen_thread(session, thread_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/{thread_id}/timeout-close", response_model=ThreadRead)
def timeout_close(thread_id: UUID, session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    thread = thread_service.get_thread(session, thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")
    check_board_permission(thread.namespace_id, session, user)
    try:
        return thread_service.timeout_close_thread(session, thread_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/{thread_id}/comments", response_model=list[CommentRead])
def list_comments(thread_id: UUID, session: Session = Depends(get_db)):
    comments = thread_service.list_comments(session, thread_id)
    author_ids = [c.author_id for c in comments if c.author_id]
    users = {}
    if author_ids:
        users = {u.id: u.display_name for u in session.exec(select(User).where(User.id.in_(author_ids))).all()}

    # 构建 comment_id → (author_id, content) 映射，用于填充回复元信息
    comment_author_map = {c.id: c.author_id for c in comments}
    comment_content_map = {c.id: c.content for c in comments}

    result = []
    for c in comments:
        d = c.model_dump()
        d["author_display_name"] = users.get(c.author_id) if c.author_id else None
        d["reply_to_author_display_name"] = _resolve_reply_author(
            c.reply_to_comment_id, comment_author_map, users,
        )
        d["reply_to_content_preview"] = _truncate_preview(
            comment_content_map.get(c.reply_to_comment_id),
        )
        result.append(d)
    return result


def _resolve_reply_author(
    reply_to_id: UUID | None,
    comment_author_map: dict,
    users: dict,
) -> str | None:
    """Resolve display name of the comment being replied to."""
    if not reply_to_id:
        return None
    parent_author_id = comment_author_map.get(reply_to_id)
    if not parent_author_id:
        return None
    return users.get(parent_author_id)


_PREVIEW_MAX_LEN = 100


def _truncate_preview(content: str | None) -> str | None:
    """Return first line of content, truncated to ~100 chars."""
    if not content:
        return None
    # Take first non-empty line, strip markdown images/links noise
    first_line = content.split("\n", 1)[0].strip()
    if len(first_line) > _PREVIEW_MAX_LEN:
        return first_line[:_PREVIEW_MAX_LEN] + "…"
    return first_line


@router.post("/{thread_id}/comments", response_model=CommentRead, status_code=201)
def add_comment(thread_id: UUID, data: CommentCreate, session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    thread = thread_service.get_thread(session, thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")
    check_namespace_write_access(thread.namespace_id, session, user)
    return thread_service.add_comment(session, data, user.id)


@router.post("/{thread_id}/ai-answer", status_code=204)
def ai_answer(thread_id: UUID, session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """提交 AI 回答生成到后台线程，立即返回 204，避免前端等待 LLM 超时。"""
    thread = thread_service.get_thread(session, thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")
    check_namespace_write_access(thread.namespace_id, session, user)
    thread_service.submit_ai_answer(thread_id)


# In-memory lock: tracks threads currently generating AI answers.
# Lock is held from request start until the LLM background thread finishes,
# NOT until the SSE connection closes.
_ai_generating: set[UUID] = set()
_ai_lock = __import__("threading").Lock()


@router.get("/{thread_id}/ai-answer/stream")
def stream_ai_answer(
    thread_id: UUID,
    force: bool = False,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """SSE endpoint: stream AI answer tokens in real-time.

    Supports "resume": if LLM is already generating for this thread
    (e.g. user refreshed the page), the new connection picks up from
    the shared TokenBuffer and continues streaming from where it left off.

    Args:
        force: True = regenerate even if AI answer exists (manual regenerate).

    Emits:
      data: {"phase":"searching"}   — searching memories / RAG
      data: {"phase":"generating"}  — LLM generation started
      data: {"token":"..."}         — incremental text chunk
      data: {"done":true}           — generation complete, comment saved
      data: {"error":"..."}         — generation failed
      data: {"skipped":true}        — AI answer already exists (non-placeholder)
    """
    thread = thread_service.get_thread(session, thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")
    check_namespace_read_access(thread.namespace_id, session, user)

    # 1) If a real (non-placeholder) AI answer exists, skip
    if not force:
        existing_ai = session.exec(
            select(Comment).where(
                Comment.thread_id == thread_id,
                Comment.is_ai.is_(True),
                Comment.deleted_at.is_(None),
            )
        ).first()
        if existing_ai and existing_ai.content != thread_service._AI_PLACEHOLDER:
            return _sse_response(iter([_sse({"skipped": True, "reason": "ai_exists"})]))

    # 2) If LLM is already generating, resume from shared buffer
    active_buf = thread_service.get_active_buffer(thread_id)
    if active_buf and not force:
        return _sse_response(_resume_stream(active_buf))

    # 3) Start new generation
    with _ai_lock:
        _ai_generating.add(thread_id)

    def _on_llm_complete():
        with _ai_lock:
            _ai_generating.discard(thread_id)

    def _new_stream():
        yield _sse({"phase": "searching"})
        try:
            buf = thread_service.start_ai_generation(
                session, thread_id, on_complete=_on_llm_complete,
            )
            yield _sse({"phase": "generating"})
            for chunk in buf.iter_from(0):
                yield _sse({"token": chunk})
            yield _sse({"done": True})
        except Exception as exc:
            logger.exception("Streaming AI answer failed for thread %s", thread_id)
            yield _sse({"error": str(exc)})

    return _sse_response(_new_stream())


def _resume_stream(buf):
    """Resume from an in-progress TokenBuffer — replay all existing + live tokens."""
    yield _sse({"phase": "generating"})
    try:
        for chunk in buf.iter_from(0):
            yield _sse({"token": chunk})
        yield _sse({"done": True})
    except Exception as exc:
        logger.exception("Resume stream failed: %s", exc)
        yield _sse({"error": str(exc)})


def _sse_response(generator):
    return StreamingResponse(
        generator,
        media_type="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.delete("/{thread_id}", status_code=204)
def delete_thread(
    thread_id: UUID,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """帖子作者可删除自己的帖子（记忆级联删除），管理员可删除任意帖子（记忆标记待审）。"""
    thread = thread_service.get_thread(session, thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")

    is_admin = user.role in (SystemRole.SUPER_ADMIN, SystemRole.BOARD_ADMIN)
    is_author = thread.author_id == user.id

    if is_admin:
        check_board_permission(thread.namespace_id, session, user)
        deleted_by_admin = True
    elif is_author:
        deleted_by_admin = False
    else:
        raise HTTPException(403, "只有帖子作者或管理员可以删除帖子")

    try:
        thread_service.delete_thread(session, thread_id, deleted_by_admin=deleted_by_admin)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/{thread_id}/comments/{comment_id}/upvote", response_model=UpvoteResponse)
def upvote_comment(thread_id: UUID, comment_id: UUID, session: Session = Depends(get_db), user_id: UUID = Depends(get_current_user_id)):
    try:
        comment, voted = thread_service.toggle_upvote(session, comment_id, user_id, thread_id=thread_id)
        return UpvoteResponse(
            id=comment.id, thread_id=comment.thread_id, upvote_count=comment.upvote_count, voted=voted,
        )
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@router.delete("/{thread_id}/comments/{comment_id}")
def delete_comment(
    thread_id: UUID,
    comment_id: UUID,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Determine if user has board admin rights for this thread's namespace
    thread = thread_service.get_thread(session, thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")

    is_board_admin = False
    if user.role == SystemRole.SUPER_ADMIN:
        is_board_admin = True
    elif user.role == SystemRole.BOARD_ADMIN:
        stmt = select(NamespaceModerator).where(
            NamespaceModerator.user_id == user.id,
            NamespaceModerator.namespace_id == thread.namespace_id,
        )
        if session.exec(stmt).first():
            is_board_admin = True

    try:
        thread = thread_service.delete_comment(session, comment_id, user.id, thread_id=thread_id, is_board_admin=is_board_admin)
        # Re-extract memories only if board admin and thread was resolved
        if is_board_admin and thread.resolved_type:
            from forum_memory.services import extraction_service
            try:
                extraction_service.re_extract(session, "thread", thread_id)
            except Exception:
                logger.exception("re_extract failed for thread %s after comment deletion", thread_id)
        return {"ok": True}
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
