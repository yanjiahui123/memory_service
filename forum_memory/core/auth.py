"""JWT token creation and verification."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt

from forum_memory.config import get_settings


def create_access_token(employee_id: str, user_id: UUID) -> dict:
    """Create a JWT access token.

    Returns {"access_token": str, "token_type": "bearer", "expires_in": int}.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expire = now + timedelta(hours=settings.jwt_expire_hours)
    payload = {
        "sub": employee_id,
        "uid": str(user_id),
        "iat": now,
        "exp": expire,
    }
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": settings.jwt_expire_hours * 3600,
    }


def decode_access_token(token: str) -> dict | None:
    """Decode and verify a JWT access token.

    Returns the payload dict on success, or None if invalid/expired.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
