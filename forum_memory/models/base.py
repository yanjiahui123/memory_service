"""Shared base model with timestamp fields."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlmodel import SQLModel, Field

_TZ8 = timezone(timedelta(hours=8))


def _now_tz8() -> datetime:
    return datetime.now(tz=_TZ8)


class UUIDMixin(SQLModel):
    """Mixin that adds a UUID primary key."""
    id: UUID = Field(default_factory=uuid4, primary_key=True)


class TimestampMixin(SQLModel):
    """Mixin that adds created_at and updated_at.

    updated_at 不使用 DB-level onupdate，避免 view_count 等无关字段的
    UPDATE 也刷新时间戳。需要刷新时由业务代码显式赋值。
    """
    created_at: datetime = Field(default_factory=_now_tz8)
    updated_at: datetime = Field(default_factory=_now_tz8)
