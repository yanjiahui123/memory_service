"""Feedback schemas."""

from uuid import UUID
from datetime import datetime
from pydantic import BaseModel


class FeedbackCreate(BaseModel):
    feedback_type: str
    comment: str | None = None


class FeedbackWithdraw(BaseModel):
    feedback_type: str


class FeedbackRead(BaseModel):
    id: UUID
    memory_id: UUID
    feedback_type: str
    comment: str | None
    created_at: datetime
    model_config = {"from_attributes": True}


class FeedbackSummary(BaseModel):
    useful: int = 0
    not_useful: int = 0
    wrong: int = 0
    outdated: int = 0
    total: int = 0
    useful_ratio: float = 0.0
