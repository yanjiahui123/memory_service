"""Notification model for user notifications."""

from uuid import UUID
from datetime import datetime

from sqlmodel import Field
from sqlalchemy import Index

from forum_memory.models.base import UUIDMixin, TimestampMixin


class Notification(UUIDMixin, TimestampMixin, table=True):
    """User notification triggered by forum interactions."""
    __tablename__ = "memo_notifications"
    __table_args__ = (
        Index("ix_notif_recipient_unread", "recipient_id", "is_read"),
    )

    recipient_id: UUID = Field(foreign_key="memo_users.id", index=True)
    actor_id: UUID = Field(foreign_key="memo_users.id")
    notification_type: str = Field(max_length=50)
    # "comment_on_thread" | "reply_to_comment"

    thread_id: UUID = Field(foreign_key="memo_threads.id", ondelete="CASCADE")
    comment_id: UUID | None = Field(
        default=None, foreign_key="memo_comments.id", ondelete="CASCADE",
    )

    is_read: bool = Field(default=False, index=True)
    read_at: datetime | None = Field(default=None)
