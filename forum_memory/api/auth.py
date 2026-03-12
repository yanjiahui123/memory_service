"""Authentication API routes — JWT token issuance."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from forum_memory.api.deps import get_db
from forum_memory.config import get_settings
from forum_memory.core.auth import create_access_token
from forum_memory.models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    employee_id: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    expires_in: int


@router.post("/login", response_model=TokenResponse)
def login(data: LoginRequest, session: Session = Depends(get_db)):
    """Authenticate by employee_id and return a JWT access token.

    Only available when jwt_enabled=True.
    """
    settings = get_settings()
    if not settings.jwt_enabled:
        raise HTTPException(400, "JWT authentication is not enabled; use X-Employee-Id header")

    employee_id = data.employee_id.strip()
    if not employee_id:
        raise HTTPException(400, "employee_id is required")

    stmt = select(User).where(User.employee_id == employee_id, User.is_active.is_(True))
    user = session.exec(stmt).first()
    if not user:
        raise HTTPException(401, f"工号 {employee_id} 未注册或已停用")

    token_data = create_access_token(employee_id, user.id)
    return TokenResponse(**token_data)
