"""Thread (post) and Comment models."""

from uuid import UUID
from datetime import datetime

from sqlmodel import Field
from sqlalchemy import Column, Text, JSON

from forum_memory.models.base import UUIDMixin, TimestampMixin
from forum_memory.models.enums import ThreadStatus, ResolvedType, Priority


class Thread(UUIDMixin, TimestampMixin, table=True):
    """Forum thread / post."""
    __tablename__ = "memo_threads"

    namespace_id: UUID = Field(foreign_key="memo_namespaces.id", index=True)
    author_id: UUID = Field(foreign_key="memo_users.id", index=True)

    title: str = Field(max_length=500)
    content: str = Field(sa_column=Column(Text, nullable=False))

    # State machine
    status: ThreadStatus = Field(default=ThreadStatus.OPEN, index=True)
    resolved_type: ResolvedType | None = Field(default=None)
    best_answer_id: UUID | None = Field(default=None)

    # Tags & metadata (stored as JSON array)
    tags: list | None = Field(default=None, sa_column=Column("tags", JSON))
    priority: Priority | None = Field(default=None)
    knowledge_type: str | None = Field(default=None, max_length=50)
    environment: str | None = Field(default=None, max_length=200)

    # Counters
    comment_count: int = Field(default=0)
    view_count: int = Field(default=0)

    # Timestamps
    resolved_at: datetime | None = Field(default=None)
    timeout_at: datetime | None = Field(default=None)


class Comment(UUIDMixin, TimestampMixin, table=True):
    """Comment / reply on a thread."""
    __tablename__ = "memo_comments"

    thread_id: UUID = Field(foreign_key="memo_threads.id", index=True)
    author_id: UUID | None = Field(default=None, foreign_key="memo_users.id")
    reply_to_comment_id: UUID | None = Field(default=None, foreign_key="memo_comments.id", index=True)
    is_ai: bool = Field(default=False)

    content: str = Field(sa_column=Column(Text, nullable=False))
    author_role: str = Field(default="commenter", max_length=50)

    # Voting
    upvote_count: int = Field(default=0)
    is_best_answer: bool = Field(default=False)

    # Soft-delete for audit trail
    deleted_at: datetime | None = Field(default=None, index=True)

    # AI-specific: memory IDs cited, RAG context used
    cited_memory_ids: list | None = Field(default=None, sa_column=Column("cited_memory_ids", JSON))
    rag_context: str | None = Field(default=None, sa_column=Column("rag_context", Text, nullable=True))
