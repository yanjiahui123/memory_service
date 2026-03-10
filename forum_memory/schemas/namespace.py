"""Namespace schemas."""

from uuid import UUID
from pydantic import BaseModel


class NamespaceCreate(BaseModel):
    display_name: str
    description: str | None = None
    access_mode: str = "public"


class NamespaceUpdate(BaseModel):
    display_name: str | None = None
    description: str | None = None
    access_mode: str | None = None
    config: dict | None = None


class NamespaceRead(BaseModel):
    id: UUID
    name: str
    display_name: str
    description: str | None
    access_mode: str
    config: dict
    dictionary: dict
    is_active: bool
    es_index_name: str | None = None
    thread_count: int = 0
    open_thread_count: int = 0
    model_config = {"from_attributes": True}


class NamespaceStats(BaseModel):
    total_threads: int = 0
    open_threads: int = 0
    resolved_threads: int = 0
    total_memories: int = 0
    locked_memories: int = 0
    ai_resolve_rate: float = 0.0
    pending_count: int = 0


class DictionaryUpdate(BaseModel):
    entries: dict
