"""Board share link models — multi-board sharing."""

from uuid import UUID

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from forum_memory.models.base import UUIDMixin, TimestampMixin


class BoardShareLink(UUIDMixin, TimestampMixin, table=True):
    """A shareable link that bundles multiple boards."""
    __tablename__ = "memo_board_share_links"

    code: str = Field(max_length=32, unique=True, index=True)
    name: str = Field(max_length=100)
    created_by: UUID = Field(foreign_key="memo_users.id")
    use_count: int = Field(default=0)
    is_active: bool = Field(default=True)


class BoardShareLinkNamespace(UUIDMixin, table=True):
    """Junction table: share link ↔ namespace."""
    __tablename__ = "memo_board_share_link_namespaces"
    __table_args__ = (
        UniqueConstraint("share_link_id", "namespace_id"),
    )

    share_link_id: UUID = Field(foreign_key="memo_board_share_links.id", index=True)
    namespace_id: UUID = Field(foreign_key="memo_namespaces.id", index=True)
