"""Memory relation model — directed edges between memories."""

from uuid import UUID

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from forum_memory.models.base import UUIDMixin, TimestampMixin
from forum_memory.models.enums import RelationType


class MemoryRelation(UUIDMixin, TimestampMixin, table=True):
    """Directed edge between two memories within the same namespace."""
    __tablename__ = "memo_memory_relations"
    __table_args__ = (
        UniqueConstraint(
            "source_memory_id", "target_memory_id", "relation_type",
            name="uq_memory_relation_triple",
        ),
    )

    source_memory_id: UUID = Field(foreign_key="memo_memories.id", index=True)
    target_memory_id: UUID = Field(foreign_key="memo_memories.id", index=True)
    relation_type: RelationType = Field(index=True)
    confidence: float = Field(default=1.0)
    origin: str = Field(default="audn", max_length=50)
