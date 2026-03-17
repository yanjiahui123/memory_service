"""Shared base model with timestamp fields."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlmodel import SQLModel, Field


class UUIDMixin(SQLModel):
    """Mixin that adds a UUID primary key."""
    id: UUID = Field(default_factory=uuid4, primary_key=True)


class TimestampMixin(SQLModel):
    """Mixin that adds created_at and updated_at."""
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone(timedelta(hours=8))))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone(timedelta(hours=8))))
