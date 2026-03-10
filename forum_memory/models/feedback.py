"""Feedback model for memory quality tracking."""

from uuid import UUID

from sqlmodel import Field

from forum_memory.models.base import UUIDMixin, TimestampMixin
from forum_memory.models.enums import FeedbackType


class Feedback(UUIDMixin, TimestampMixin, table=True):
    """User feedback on a memory."""
    __tablename__ = "feedbacks"

    memory_id: UUID = Field(foreign_key="memories.id", index=True)
    user_id: UUID | None = Field(default=None, foreign_key="users.id")
    feedback_type: FeedbackType = Field(index=True)
    comment: str | None = Field(default=None, max_length=1000)
