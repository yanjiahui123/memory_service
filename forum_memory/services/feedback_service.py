"""Feedback service — sync."""

from uuid import UUID

from sqlmodel import Session, select, func
from sqlalchemy import update as sa_update

from forum_memory.models.feedback import Feedback
from forum_memory.models.memory import Memory
from forum_memory.models.enums import FeedbackType
from forum_memory.schemas.feedback import FeedbackCreate, FeedbackSummary
from forum_memory.services.memory_service import refresh_quality


def submit_feedback(session: Session, memory_id: UUID, data: FeedbackCreate, user_id: UUID | None = None) -> Feedback:
    new_type = FeedbackType(data.feedback_type)

    if user_id:
        # Prevent duplicate: same user + same memory + same feedback type
        existing = session.exec(
            select(Feedback).where(
                Feedback.memory_id == memory_id,
                Feedback.user_id == user_id,
                Feedback.feedback_type == new_type,
            )
        ).first()
        if existing:
            return existing  # Idempotent — don't double-count

        # Auto-withdraw other feedback types from same user on same memory
        _withdraw_other_types(session, memory_id, user_id, new_type)

    fb = Feedback(
        memory_id=memory_id,
        user_id=user_id,
        feedback_type=new_type,
        comment=data.comment,
    )
    session.add(fb)
    _update_counter(session, memory_id, data.feedback_type)
    session.commit()
    session.refresh(fb)
    refresh_quality(session, memory_id)
    return fb


def _withdraw_other_types(
    session: Session, memory_id: UUID, user_id: UUID, keep_type: FeedbackType,
) -> None:
    """Remove any existing feedback of different types from same user on same memory."""
    stmt = select(Feedback).where(
        Feedback.memory_id == memory_id,
        Feedback.user_id == user_id,
        Feedback.feedback_type != keep_type,
    )
    old_feedbacks = list(session.exec(stmt).all())
    for old_fb in old_feedbacks:
        _decrement_counter(session, memory_id, old_fb.feedback_type.value)
        session.delete(old_fb)


def list_feedback(session: Session, memory_id: UUID) -> list[Feedback]:
    stmt = select(Feedback).where(Feedback.memory_id == memory_id).order_by(Feedback.created_at.desc())
    return list(session.exec(stmt).all())


def get_summary(session: Session, memory_id: UUID) -> FeedbackSummary:
    # Single GROUP BY query instead of N separate COUNT queries
    stmt = (
        select(Feedback.feedback_type, func.count())
        .where(Feedback.memory_id == memory_id)
        .group_by(Feedback.feedback_type)
    )
    rows = session.exec(stmt).all()
    counts = {row[0].value: row[1] for row in rows}

    total = sum(counts.values())
    useful = counts.get("useful", 0)
    ratio = useful / total if total > 0 else 0.0

    return FeedbackSummary(
        useful=useful,
        not_useful=counts.get("not_useful", 0),
        wrong=counts.get("wrong", 0),
        outdated=counts.get("outdated", 0),
        total=total,
        useful_ratio=round(ratio, 4),
    )


def get_my_feedback(session: Session, memory_id: UUID, user_id: UUID) -> str | None:
    """Return the current user's feedback type on a memory, or None."""
    fb = session.exec(
        select(Feedback.feedback_type).where(
            Feedback.memory_id == memory_id,
            Feedback.user_id == user_id,
        )
    ).first()
    return fb.value if fb else None


def withdraw_feedback(session: Session, memory_id: UUID, feedback_type: str, user_id: UUID) -> bool:
    """Remove a user's own feedback on a memory. Returns True if feedback was found and removed."""
    stmt = select(Feedback).where(
        Feedback.memory_id == memory_id,
        Feedback.feedback_type == FeedbackType(feedback_type),
        Feedback.user_id == user_id,
    )
    fb = session.exec(stmt.order_by(Feedback.created_at.desc())).first()
    if not fb:
        return False
    session.delete(fb)
    _decrement_counter(session, memory_id, feedback_type)
    session.commit()
    refresh_quality(session, memory_id)
    return True


_COUNTER_MAP = {
    "useful": "useful_count",
    "not_useful": "not_useful_count",
    "wrong": "wrong_count",
    "outdated": "outdated_count",
}


def _decrement_counter(session: Session, memory_id: UUID, feedback_type: str) -> None:
    attr = _COUNTER_MAP.get(feedback_type)
    if not attr:
        return
    column = getattr(Memory, attr)
    stmt = (
        sa_update(Memory)
        .where(Memory.id == memory_id)
        .values(**{attr: func.greatest(column - 1, 0)})
    )
    session.exec(stmt)


def _update_counter(session: Session, memory_id: UUID, feedback_type: str) -> None:
    attr = _COUNTER_MAP.get(feedback_type)
    if not attr:
        return
    column = getattr(Memory, attr)
    stmt = (
        sa_update(Memory)
        .where(Memory.id == memory_id)
        .values(**{attr: column + 1})
    )
    session.exec(stmt)
