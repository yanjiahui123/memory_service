"""Notification service — create, query, and manage user notifications."""

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlmodel import Session, select, func
from sqlalchemy import and_, update as sa_update

from forum_memory.models.notification import Notification
from forum_memory.models.enums import ThreadStatus
from forum_memory.models.thread import Thread, Comment
from forum_memory.models.user import User
from forum_memory.models.namespace_moderator import NamespaceModerator

logger = logging.getLogger(__name__)

_TZ = timezone(timedelta(hours=8))


# ── Creation ─────────────────────────────────────────────────────────────────


def create_notification(
    session: Session,
    recipient_id: UUID,
    actor_id: UUID,
    notification_type: str,
    thread_id: UUID,
    comment_id: UUID | None = None,
) -> Notification | None:
    """Create a notification record. Skips self-notification (actor == recipient)."""
    if recipient_id == actor_id:
        return None
    notif = Notification(
        recipient_id=recipient_id,
        actor_id=actor_id,
        notification_type=notification_type,
        thread_id=thread_id,
        comment_id=comment_id,
    )
    session.add(notif)
    return notif


def notify_on_comment(session: Session, comment: Comment, thread: Thread) -> None:
    """Create notifications for a new comment.

    1. Notify thread author (type=comment_on_thread).
    2. If replying to a comment, also notify that comment's author (type=reply_to_comment).
    Duplicates are prevented via notified_ids set.
    """
    if comment.is_ai or not comment.author_id:
        return

    notified: set[UUID] = set()

    # Notify thread author
    if thread.author_id and thread.author_id != comment.author_id:
        create_notification(
            session, thread.author_id, comment.author_id,
            "comment_on_thread", thread.id, comment.id,
        )
        notified.add(thread.author_id)

    # Notify parent comment author (on reply)
    _notify_reply_target(session, comment, notified)


def _notify_reply_target(
    session: Session, comment: Comment, notified: set[UUID],
) -> None:
    """Notify the author of the comment being replied to, if not already notified."""
    if not comment.reply_to_comment_id or not comment.author_id:
        return
    parent = session.get(Comment, comment.reply_to_comment_id)
    if not parent or not parent.author_id:
        return
    if parent.author_id in notified:
        return
    create_notification(
        session, parent.author_id, comment.author_id,
        "reply_to_comment", parent.thread_id, comment.id,
    )


def notify_admins_on_new_thread(session: Session, thread: Thread) -> None:
    """Notify all namespace moderators when a new thread is created.

    Skips the thread author if they are also a moderator.
    """
    stmt = select(NamespaceModerator.user_id).where(
        NamespaceModerator.namespace_id == thread.namespace_id,
    )
    mod_user_ids = list(session.exec(stmt).all())
    for mod_id in mod_user_ids:
        create_notification(
            session, mod_id, thread.author_id,
            "new_thread_in_namespace", thread.id,
        )


# ── Queries ──────────────────────────────────────────────────────────────────


def _thread_alive_clause():
    """Return a single join-ON clause that excludes notifications for deleted threads."""
    return and_(Notification.thread_id == Thread.id, Thread.status != ThreadStatus.DELETED)


def get_unread_count(session: Session, user_id: UUID) -> int:
    """Count unread notifications for a user (excludes deleted threads)."""
    stmt = (
        select(func.count())
        .select_from(Notification)
        .join(Thread, _thread_alive_clause())
        .where(Notification.recipient_id == user_id, Notification.is_read.is_(False))
    )
    return session.exec(stmt).one()


def list_notifications(
    session: Session,
    user_id: UUID,
    page: int = 1,
    size: int = 20,
    unread_only: bool = False,
) -> tuple[list[dict], int]:
    """List notifications with enriched actor and thread info. Returns (items, total)."""
    base = (
        select(Notification)
        .join(Thread, _thread_alive_clause())
        .where(Notification.recipient_id == user_id)
    )
    if unread_only:
        base = base.where(Notification.is_read.is_(False))

    total = _count_notifications(session, user_id, unread_only)

    stmt = base.order_by(Notification.created_at.desc()).offset((page - 1) * size).limit(size)
    notifs = list(session.exec(stmt).all())
    if not notifs:
        return [], total

    return _enrich_notifications(session, notifs), total


def _count_notifications(
    session: Session, user_id: UUID, unread_only: bool,
) -> int:
    """Count total notifications matching filters (excludes deleted threads)."""
    stmt = (
        select(func.count())
        .select_from(Notification)
        .join(Thread, _thread_alive_clause())
        .where(Notification.recipient_id == user_id)
    )
    if unread_only:
        stmt = stmt.where(Notification.is_read.is_(False))
    return session.exec(stmt).one()


def _enrich_notifications(
    session: Session, notifs: list[Notification],
) -> list[dict]:
    """Batch-join actor display names and thread titles onto notification dicts."""
    actor_ids = {n.actor_id for n in notifs}
    thread_ids = {n.thread_id for n in notifs}

    actors = _batch_display_names(session, actor_ids)
    titles = _batch_thread_titles(session, thread_ids)

    result = []
    for n in notifs:
        d = n.model_dump()
        d["actor_display_name"] = actors.get(n.actor_id)
        d["thread_title"] = titles.get(n.thread_id)
        result.append(d)
    return result


def _batch_display_names(session: Session, user_ids: set[UUID]) -> dict[UUID, str]:
    """Fetch display names for a set of user IDs."""
    if not user_ids:
        return {}
    users = session.exec(select(User).where(User.id.in_(user_ids))).all()
    return {u.id: u.display_name for u in users}


def _batch_thread_titles(session: Session, thread_ids: set[UUID]) -> dict[UUID, str]:
    """Fetch thread titles for a set of thread IDs."""
    if not thread_ids:
        return {}
    threads = session.exec(select(Thread).where(Thread.id.in_(thread_ids))).all()
    return {t.id: t.title for t in threads}


# ── Mutations ────────────────────────────────────────────────────────────────


def mark_as_read(session: Session, notification_id: UUID, user_id: UUID) -> bool:
    """Mark a single notification as read. Returns False if not found / not owned."""
    notif = session.get(Notification, notification_id)
    if not notif or notif.recipient_id != user_id:
        return False
    notif.is_read = True
    notif.read_at = datetime.now(tz=_TZ)
    session.commit()
    return True


def mark_all_as_read(session: Session, user_id: UUID) -> int:
    """Mark all unread notifications as read. Returns count updated."""
    now = datetime.now(tz=_TZ)
    result = session.execute(
        sa_update(Notification)
        .where(Notification.recipient_id == user_id, Notification.is_read.is_(False))
        .values(is_read=True, read_at=now)
    )
    session.commit()
    return result.rowcount
