"""User board follow/subscription model."""

from uuid import UUID

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from forum_memory.models.base import UUIDMixin, TimestampMixin


class BoardFollow(UUIDMixin, TimestampMixin, table=True):
    """Tracks which boards a user has followed/subscribed to."""

    __tablename__ = "memo_user_board_follows"
    __table_args__ = (
        UniqueConstraint("user_id", "namespace_id", name="uq_user_board_follow"),
    )

    user_id: UUID = Field(foreign_key="memo_users.id", index=True)
    namespace_id: UUID = Field(foreign_key="memo_namespaces.id", index=True)
