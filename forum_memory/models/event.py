"""Domain event log."""

from uuid import UUID

from sqlmodel import Field
from sqlalchemy import Column, Text, JSON

from forum_memory.models.base import UUIDMixin, TimestampMixin


class DomainEvent(UUIDMixin, TimestampMixin, table=True):
    """Domain events for async processing and audit."""
    __tablename__ = "domain_events"

    event_type: str = Field(max_length=100, index=True)
    aggregate_type: str = Field(max_length=100)
    aggregate_id: UUID = Field(index=True)
    namespace_id: UUID | None = Field(default=None, foreign_key="namespaces.id")
    payload: dict = Field(default_factory=dict, sa_column=Column("payload", JSON, nullable=False, server_default="{}"))
    processed: bool = Field(default=False, index=True)
