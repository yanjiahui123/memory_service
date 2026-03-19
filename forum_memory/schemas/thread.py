"""Thread and comment schemas."""

from uuid import UUID
from datetime import datetime
from pydantic import BaseModel


class ThreadCreate(BaseModel):
    namespace_id: UUID
    title: str
    content: str
    tags: list[str] | None = None
    knowledge_type: str | None = None
    environment: str | None = None
    priority: str | None = None


class ThreadRead(BaseModel):
    id: UUID
    namespace_id: UUID
    author_display_name: str | None = None
    title: str
    content: str
    status: str
    resolved_type: str | None
    tags: list | None
    priority: str | None
    knowledge_type: str | None
    environment: str | None
    comment_count: int
    view_count: int
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class ThreadResolve(BaseModel):
    best_answer_id: UUID | None = None


class CommentCreate(BaseModel):
    thread_id: UUID
    content: str
    reply_to_comment_id: UUID | None = None


class CommentRead(BaseModel):
    id: UUID
    thread_id: UUID
    author_id: UUID | None = None
    author_display_name: str | None = None
    reply_to_comment_id: UUID | None = None
    reply_to_author_display_name: str | None = None
    content: str
    author_role: str
    is_ai: bool
    upvote_count: int
    is_best_answer: bool
    cited_memory_ids: list | None
    rag_context: str | None = None
    created_at: datetime
    model_config = {"from_attributes": True}


class UpvoteResponse(BaseModel):
    id: UUID
    thread_id: UUID
    upvote_count: int
    voted: bool
