"""Namespace (board/section) model."""

from uuid import UUID

from sqlmodel import Field
from sqlalchemy import Column, JSON

from forum_memory.models.base import UUIDMixin, TimestampMixin


class Namespace(UUIDMixin, TimestampMixin, table=True):
    """Forum board / knowledge namespace."""
    __tablename__ = "namespaces"

    name: str = Field(max_length=200, unique=True, index=True)
    display_name: str = Field(max_length=200)
    description: str | None = Field(default=None)
    owner_id: UUID = Field(foreign_key="users.id", index=True)
    access_mode: str = Field(default="public", max_length=20)
    config: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False, server_default="{}"))
    dictionary: dict = Field(default_factory=dict, sa_column=Column("dictionary", JSON, nullable=False, server_default="{}"))
    is_active: bool = Field(default=True)
    es_index_name: str | None = Field(default=None, max_length=200)
