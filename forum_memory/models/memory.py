"""Memory (knowledge unit) model."""

from uuid import UUID
from datetime import datetime

from sqlmodel import Field
from sqlalchemy import Column, Text, JSON

from forum_memory.models.base import UUIDMixin, TimestampMixin
from forum_memory.models.enums import Authority, MemoryStatus, KnowledgeType


class Memory(UUIDMixin, TimestampMixin, table=True):
    """Single knowledge unit extracted from resolved threads."""
    __tablename__ = "memo_memories"

    namespace_id: UUID = Field(foreign_key="memo_namespaces.id", index=True)

    # Content
    content: str = Field(sa_column=Column(Text, nullable=False))

    # Two-dimensional state
    authority: Authority = Field(default=Authority.NORMAL, index=True)
    status: MemoryStatus = Field(default=MemoryStatus.ACTIVE, index=True)

    # Quality
    quality_score: float = Field(default=0.5)

    # Classification
    knowledge_type: str | None = Field(default=None, max_length=50)
    tags: list | None = Field(default=None, sa_column=Column("tags", JSON))
    environment: str | None = Field(default=None, max_length=200)

    # Source tracing
    source_type: str = Field(default="thread", max_length=50)
    source_id: UUID | None = Field(default=None, index=True)
    source_role: str | None = Field(default=None, max_length=50)
    resolved_type: str | None = Field(default=None, max_length=50)

    # Feedback counters
    useful_count: int = Field(default=0)
    not_useful_count: int = Field(default=0)
    wrong_count: int = Field(default=0)
    outdated_count: int = Field(default=0)

    # Retrieval stats
    retrieve_count: int = Field(default=0)
    cite_count: int = Field(default=0)
    resolved_citation_count: int = Field(default=0)  # 引用此记忆后帖子被解决的次数
    last_retrieved_at: datetime | None = Field(default=None)

    # ES sync tracking — NULL means ES index pending/failed
    indexed_at: datetime | None = Field(default=None)

    # Human confirmation flag
    pending_human_confirm: bool = Field(default=False)

    # Flexible extra data
    extra: dict = Field(default_factory=dict, sa_column=Column("extra", JSON, nullable=False, server_default="{}"))
