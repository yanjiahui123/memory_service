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

    updated_at is automatically refreshed on every UPDATE via DB-level onupdate.
    """
    created_at: datetime = Field(default_factory=_now_tz8)
    updated_at: datetime = Field(
        default_factory=_now_tz8,
        sa_column_kwargs={"onupdate": _now_tz8},
    )
