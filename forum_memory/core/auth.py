"""JWT token creation and verification, SSO cookie verification."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import jwt
import requests

from forum_memory.config import get_settings

logger = logging.getLogger(__name__)


def create_access_token(employee_id: str, user_id: UUID) -> dict:
    """Create a JWT access token.

    Returns {"access_token": str, "token_type": "bearer", "expires_in": int}.
    """
    settings = get_settings()
    now = datetime.now(tz=timezone(timedelta(hours=8)))
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


# ---------------------------------------------------------------------------
# SSO Cookie verification
# ---------------------------------------------------------------------------

def _sign_sso_jwt(ak: str, sk: str) -> str:
    """Create a short-lived JWT for authenticating with the SSO verification API."""
    expires_at = datetime.now(tz=timezone(timedelta(hours=8))) + timedelta(minutes=10)
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"accessKeyId": ak, "exp": expires_at}
    return jwt.encode(payload, sk, algorithm="HS256", headers=header)


def verify_sso_cookie(cookies: dict[str, str]) -> dict[str, Any] | None:
    """Verify SSO cookies against the external verification API.

    Args:
        cookies: Dict containing hwsso_login, hwssot3, login_sid, login_uid.

    Returns:
        user_info dict on success (contains uid, displayNameCn, email, etc.),
        or None if cookies are missing / verification fails.
    """
    hwsso_login = cookies.get("hwsso_login")
    hwssot3 = cookies.get("hwssot3")
    login_sid = cookies.get("login_sid")
    login_uid = cookies.get("login_uid")

    if not all([hwsso_login, hwssot3, login_sid, login_uid]):
        return None

    settings = get_settings()
    if not settings.sso_enabled:
        return None

    sso_jwt = _sign_sso_jwt(settings.sso_ak, settings.sso_sk)
    headers = {
        "SSO-JWT-Authorization": sso_jwt,
        "tenantid": settings.sso_tenant_id,
    }
    body = {
        "token": {
            "hwsso_login": hwsso_login,
            "hwssot3": hwssot3,
            "login_sid": login_sid,
            "login_uid": login_uid,
        },
        "url": settings.sso_callback_url,
        "userScope": settings.sso_user_scope,
    }

    try:
        resp = requests.post(settings.sso_verify_url, headers=headers, json=body, verify=False, timeout=10)
    except requests.RequestException as e:
        logger.error("SSO cookie verification request failed: %s", e)
        return None

    if not resp.ok:
        logger.error("SSO cookie verification HTTP %s: %s", resp.status_code, resp.text[:200])
        return None

    data = resp.json()
    if data.get("errorCode"):
        logger.error("SSO cookie verification error: %s - %s", data.get("errorCode"), data.get("errorMsg"))
        return None

    return data
