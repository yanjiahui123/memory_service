"""User schemas."""

from uuid import UUID
from datetime import datetime
from pydantic import BaseModel


class UserCreate(BaseModel):
    employee_id: str
    username: str
    display_name: str
    email: str | None = None
    role: str = "user"


class UserRead(BaseModel):
    id: UUID
    employee_id: str
    username: str
    display_name: str
    email: str | None = None
    role: str
    is_active: bool
    created_at: datetime
    model_config = {"from_attributes": True}