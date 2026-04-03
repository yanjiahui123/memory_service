"""Namespace membership model (moderator + member roles)."""

from uuid import UUID

from sqlmodel import Field, UniqueConstraint

from forum_memory.models.base import UUIDMixin, TimestampMixin


class NamespaceModerator(UUIDMixin, TimestampMixin, table=True):
    """Links a user to a namespace with a role (moderator or member)."""
    __tablename__ = "memo_namespace_moderators"
    __table_args__ = (
        UniqueConstraint("user_id", "namespace_id", name="uq_user_namespace"),
    )

    user_id: UUID = Field(foreign_key="memo_users.id", index=True)
    namespace_id: UUID = Field(foreign_key="memo_namespaces.id", index=True)
    role: str = Field(default="moderator", max_length=20, index=True)
