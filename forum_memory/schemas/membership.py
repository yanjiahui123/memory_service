"""Schemas for namespace membership and invite management."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


# 单批操作的成员/用户数量上限，避免单请求触发 N×SQL
_BATCH_MAX = 200


# ── Member schemas ───────────────────────────────────────────

class MemberAdd(BaseModel):
    employee_id: str = Field(min_length=1, max_length=64)
    role: str = "member"


class MemberBatchAdd(BaseModel):
    employee_ids: list[str] = Field(min_length=1, max_length=_BATCH_MAX)
    role: str = "member"


class MemberBatchByDept(BaseModel):
    dept_code: str = Field(min_length=1, max_length=64)
    role: str = "member"


class MemberRead(BaseModel):
    user_id: UUID
    employee_id: str
    display_name: str
    dept_path: str | None = None
    role: str
    joined_at: datetime
    model_config = {"from_attributes": True}


class MemberBatchDelete(BaseModel):
    user_ids: list[UUID] = Field(min_length=1, max_length=_BATCH_MAX)


class MemberRoleUpdate(BaseModel):
    role: str


# ── Invite schemas ───────────────────────────────────────────

class InviteCreate(BaseModel):
    role: str = "member"
    max_uses: int | None = None
    expires_hours: int | None = 168


class InviteRead(BaseModel):
    id: UUID
    code: str
    role: str
    max_uses: int | None
    use_count: int
    expires_at: datetime | None
    is_active: bool
    created_at: datetime
    model_config = {"from_attributes": True}
