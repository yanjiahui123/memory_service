"""Comment vote tracking for toggle upvote/downvote."""

from uuid import UUID

from sqlmodel import Field
from sqlalchemy import UniqueConstraint

from forum_memory.models.base import UUIDMixin, TimestampMixin


class CommentVote(UUIDMixin, TimestampMixin, table=True):
    """Tracks individual user votes on comments."""
    __tablename__ = "comment_votes"
    __table_args__ = (
        UniqueConstraint("comment_id", "user_id", name="uq_comment_user_vote"),
    )

    comment_id: UUID = Field(foreign_key="comments.id", index=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)
