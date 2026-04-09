"""Schemas for namespace membership and invite management."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


# ── Member schemas ───────────────────────────────────────────

class MemberAdd(BaseModel):
    employee_id: str
    role: str = "member"


class MemberBatchAdd(BaseModel):
    employee_ids: list[str]
    role: str = "member"


class MemberBatchByDept(BaseModel):
    dept_code: str
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
    user_ids: list[UUID]


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
