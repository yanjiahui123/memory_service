"""Memory schemas."""

from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, Field


# 单条记忆字段上限（与 ES dense_vector / PG TEXT 的实用边界对齐）
_CONTENT_MAX = 10_000
_TAGS_MAX = 20


class MemoryCreate(BaseModel):
    namespace_id: UUID
    content: str = Field(min_length=1, max_length=_CONTENT_MAX)
    knowledge_type: str | None = Field(default=None, max_length=64)
    tags: list[str] | None = Field(default=None, max_length=_TAGS_MAX)
    environment: str | None = Field(default=None, max_length=64)
    source_type: str = Field(default="manual", max_length=32)
    source_id: UUID | None = None
    source_role: str | None = Field(default=None, max_length=32)
    resolved_type: str | None = Field(default=None, max_length=32)
    authority: str | None = Field(default=None, max_length=32)
    pending_human_confirm: bool = False
    pending_reason: str | None = Field(default=None, max_length=64)
    gate_confidence: float | None = None


class MemoryUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=_CONTENT_MAX)
    knowledge_type: str | None = Field(default=None, max_length=64)
    tags: list[str] | None = Field(default=None, max_length=_TAGS_MAX)
    environment: str | None = Field(default=None, max_length=64)


class MemoryRead(BaseModel):
    id: UUID
    namespace_id: UUID
    content: str
    authority: str
    status: str
    quality_score: float
    gate_confidence: float = 0.5
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
    resolved_citation_count: int = 0
    pending_human_confirm: bool
    pending_reason: str | None = None
    indexed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class AuthorityChange(BaseModel):
    authority: str
    reason: str | None = None


class MemoryBatchRequest(BaseModel):
    ids: list[UUID] = Field(min_length=1, max_length=200)


class MemoryFilter(BaseModel):
    """Bundled filter parameters for list_memories / count_memories."""
    namespace_id: UUID | None = None
    authority: str | None = None
    status: str | None = None
    pending_confirm: bool | None = None
    pending_review: bool | None = None
    pending_reason: str | None = None  # 单值或逗号分隔多值（如 "AUDN_CONFLICT,AUDN_SUPPLEMENT_LOCKED"）
    knowledge_type: str | None = None
    tags: str | None = None
    q: str | None = None
    source_id: UUID | None = None
    quality_score_max: float | None = None


class MemorySearchRequest(BaseModel):
    query: str
    namespace_id: UUID
    top_k: int = 5
    environment: str | None = None


class RelatedMemoryHint(BaseModel):
    relation_type: str
    label: str
    memory_id: UUID
    content_preview: str
    confidence: float = 1.0
    authority: str | None = None


class MemorySearchHit(BaseModel):
    memory: MemoryRead
    score: float
    env_match: bool = True
    env_warning: str | None = None
    related: list[RelatedMemoryHint] = []


class MemorySearchResponse(BaseModel):
    hits: list[MemorySearchHit]
    query_expanded: str
    total_recalled: int = 0
