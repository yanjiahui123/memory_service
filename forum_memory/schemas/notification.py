"""Notification schemas."""

from uuid import UUID
from datetime import datetime
from pydantic import BaseModel


class NotificationRead(BaseModel):
    id: UUID
    notification_type: str
    actor_display_name: str | None = None
    thread_id: UUID
    thread_title: str | None = None
    comment_id: UUID | None = None
    is_read: bool
    created_at: datetime
    model_config = {"from_attributes": True}
