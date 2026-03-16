"""Operation log for memory audit trail."""

from uuid import UUID

from sqlmodel import Field
from sqlalchemy import Column, Text, JSON

from forum_memory.models.base import UUIDMixin, TimestampMixin
from forum_memory.models.enums import OperationType


class OperationLog(UUIDMixin, TimestampMixin, table=True):
    """Audit log for every memory mutation."""
    __tablename__ = "memo_operation_logs"

    memory_id: UUID = Field(foreign_key="memo_memories.id", index=True)
    operation: OperationType = Field(index=True)
    operator_id: UUID | None = Field(default=None, foreign_key="memo_users.id")
    operator_type: str = Field(default="system", max_length=50)
    reason: str | None = Field(default=None, max_length=500)
    before_snapshot: dict | None = Field(default=None, sa_column=Column("before_snapshot", JSON))
    after_snapshot: dict | None = Field(default=None, sa_column=Column("after_snapshot", JSON))
