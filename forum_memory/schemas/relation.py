"""Memory relation schemas."""

from uuid import UUID
from datetime import datetime

from pydantic import BaseModel


class RelationCreate(BaseModel):
    target_memory_id: UUID
    relation_type: str
    confidence: float = 1.0


class RelationRead(BaseModel):
    id: UUID
    source_memory_id: UUID
    target_memory_id: UUID
    relation_type: str
    confidence: float
    origin: str
    created_at: datetime
    model_config = {"from_attributes": True}
