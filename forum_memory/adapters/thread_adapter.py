"""ThreadSourceAdapter — forum thread knowledge source.

Extracts knowledge from resolved forum threads.  This adapter encapsulates
all Thread-specific logic (loading thread + comments, determining authority
from resolved_type, building discussion text) so the extraction pipeline
can remain source-agnostic.
"""

from uuid import UUID

from sqlalchemy import text as sa_text
from sqlmodel import Session, select

from forum_memory.core.source_adapter import SourceAdapter
from forum_memory.core.source_context import SourceContext
from forum_memory.core.state_machine import default_authority, needs_human_confirm
from forum_memory.models.thread import Thread, Comment


class ThreadSourceAdapter(SourceAdapter):
    """Adapter for forum Thread sources."""

    def source_type(self) -> str:
        return "thread"

    def event_types(self) -> tuple[str, ...]:
        return ("thread.resolved", "thread.timeout_closed")

    def load_context(self, session: Session, source_id: UUID) -> SourceContext | None:
        thread = session.get(Thread, source_id)
        if not thread or not thread.resolved_type:
            return None

        discussion = _build_discussion(session, thread.id)
        role = _best_answer_role(session, thread)
        authority = default_authority(thread.resolved_type)
        pending = needs_human_confirm(thread.resolved_type)

        return SourceContext(
            source_type="thread",
            source_id=thread.id,
            namespace_id=thread.namespace_id,
            title=thread.title,
            question=thread.content,
            discussion=discussion,
            authority=authority,
            pending_human_confirm=pending,
            environment=thread.environment,
            source_role=role,
            resolved_type=thread.resolved_type,
        )

    def lock_for_re_extract(self, session: Session, source_id: UUID) -> None:
        session.execute(
            sa_text("SELECT id FROM memo_threads WHERE id = :sid FOR UPDATE NOWAIT"),
            {"sid": str(source_id)},
        )


def _build_discussion(session: Session, thread_id: UUID) -> str:
    """Build formatted discussion text from thread comments."""
    stmt = (
        select(Comment)
        .where(Comment.thread_id == thread_id, Comment.deleted_at.is_(None))
        .order_by(Comment.created_at)
    )
    comments = list(session.exec(stmt).all())
    parts = []
    for c in comments:
        role = "AI" if c.is_ai else c.author_role
        best = " [BEST]" if c.is_best_answer else ""
        parts.append(f"[{role}{best}]: {c.content}")
    return "\n\n".join(parts)


def _best_answer_role(session: Session, thread: Thread) -> str:
    """Determine the role of the best answer author."""
    if not thread.best_answer_id:
        return "unknown"
    comment = session.get(Comment, thread.best_answer_id)
    return comment.author_role if comment else "unknown"
