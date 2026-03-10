"""Extraction record for idempotent processing."""

from uuid import UUID

from sqlmodel import Field

from forum_memory.models.base import UUIDMixin, TimestampMixin
from forum_memory.models.enums import ExtractionStatus


class ExtractionRecord(UUIDMixin, TimestampMixin, table=True):
    """Tracks extraction jobs to prevent duplicates."""
    __tablename__ = "extraction_records"

    thread_id: UUID = Field(foreign_key="threads.id", unique=True, index=True)
    namespace_id: UUID = Field(foreign_key="namespaces.id", index=True)
    status: ExtractionStatus = Field(default=ExtractionStatus.PENDING)
    memory_ids_created: str | None = Field(default=None)
    error_message: str | None = Field(default=None)
