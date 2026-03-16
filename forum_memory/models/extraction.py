"""Extraction record for idempotent processing."""

from uuid import UUID

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from forum_memory.models.base import UUIDMixin, TimestampMixin
from forum_memory.models.enums import ExtractionStatus


class ExtractionRecord(UUIDMixin, TimestampMixin, table=True):
    """Tracks extraction jobs to prevent duplicates."""
    __tablename__ = "memo_extraction_records"
    __table_args__ = (
        UniqueConstraint("source_type", "source_id", name="uq_extraction_source"),
    )

    source_type: str = Field(default="thread", max_length=50, index=True)
    source_id: UUID = Field(index=True)
    namespace_id: UUID = Field(foreign_key="memo_namespaces.id", index=True)
    status: ExtractionStatus = Field(default=ExtractionStatus.PENDING)
    memory_ids_created: str | None = Field(default=None)
    error_message: str | None = Field(default=None)
