"""User model."""

from sqlmodel import Field
from forum_memory.models.base import UUIDMixin, TimestampMixin
from forum_memory.models.enums import SystemRole


class User(UUIDMixin, TimestampMixin, table=True):
    """Forum user."""
    __tablename__ = "memo_users"

    employee_id: str = Field(max_length=20, unique=True, index=True,
                             description="工号，如 00000000、00000001")
    username: str = Field(max_length=100, unique=True, index=True)
    display_name: str = Field(max_length=200)
    email: str | None = Field(default=None, max_length=200, unique=True)
    avatar_url: str | None = Field(default=None, max_length=500)
    role: SystemRole = Field(default=SystemRole.USER, index=True)
    is_active: bool = Field(default=True)