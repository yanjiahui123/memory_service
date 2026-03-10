"""Memory schemas."""

from uuid import UUID
from datetime import datetime
from pydantic import BaseModel


class MemoryCreate(BaseModel):
    namespace_id: UUID
    content: str
    knowledge_type: str | None = None
    tags: list[str] | None = None
    environment: str | None = None
    source_type: str = "manual"
    source_id: UUID | None = None
    source_role: str | None = None
    resolved_type: str | None = None
    authority: str | None = None
    pending_human_confirm: bool = False


class MemoryUpdate(BaseModel):
    content: str | None = None
    knowledge_type: str | None = None
    tags: list[str] | None = None
    environment: str | None = None


class MemoryRead(BaseModel):
    id: UUID
    namespace_id: UUID
    content: str
    authority: str
    status: str
    quality_score: float
    knowledge_type: str | None
    tags: list | None
    environment: str | None
    source_type: str
    source_id: UUID | None
    source_role: str | None
    resolved_type: str | None
    useful_count: int
    not_useful_count: int
    wrong_count: int
    outdated_count: int
    retrieve_count: int
    cite_count: int
    pending_human_confirm: bool
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class AuthorityChange(BaseModel):
    authority: str
    reason: str | None = None


class MemoryBatchRequest(BaseModel):
    ids: list[UUID]


class MemorySearchRequest(BaseModel):
    query: str
    namespace_id: UUID
    top_k: int = 5
    environment: str | None = None


class MemorySearchHit(BaseModel):
    memory: MemoryRead
    score: float
    env_match: bool = True
    env_warning: str | None = None


class MemorySearchResponse(BaseModel):
    hits: list[MemorySearchHit]
    query_expanded: str
    total_recalled: int = 0
