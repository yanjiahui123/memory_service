"""Namespace invite link model."""

from datetime import datetime
from uuid import UUID

from sqlmodel import Field

from forum_memory.models.base import UUIDMixin, TimestampMixin


class NamespaceInvite(UUIDMixin, TimestampMixin, table=True):
    """Invite link for joining a namespace."""
    __tablename__ = "memo_namespace_invites"

    namespace_id: UUID = Field(foreign_key="memo_namespaces.id", index=True)
    created_by: UUID = Field(foreign_key="memo_users.id")
    code: str = Field(max_length=32, unique=True, index=True)
    role: str = Field(default="member", max_length=20)
    max_uses: int | None = Field(default=None)
    use_count: int = Field(default=0)
    expires_at: datetime | None = Field(default=None)
    is_active: bool = Field(default=True)
