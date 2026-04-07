"""Schemas for board share link management."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class ShareLinkCreate(BaseModel):
    name: str
    namespace_ids: list[str]


class ShareLinkNamespaceInfo(BaseModel):
    namespace_id: str
    display_name: str


class ShareLinkRead(BaseModel):
    id: UUID
    code: str
    name: str
    use_count: int
    is_active: bool
    created_at: datetime
    namespaces: list[ShareLinkNamespaceInfo]


class ShareLinkInfo(BaseModel):
    """Public-facing info returned by GET /share-links/code/{code}."""
    code: str
    name: str
    namespaces: list[ShareLinkNamespaceInfo]
