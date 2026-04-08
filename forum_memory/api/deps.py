import logging
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Query, Request
from sqlmodel import Session, select

from forum_memory.database import get_session
from forum_memory.models.user import User
from forum_memory.models.namespace_moderator import NamespaceModerator
from forum_memory.models.enums import SystemRole, AccessMode, MemberRole

logger = logging.getLogger(__name__)


def get_db() -> Session:
    """Alias for database session dependency."""
    yield from get_session()


def _resolve_user_from_jwt(authorization: str, session: Session) -> User | None:
    """Try to resolve user from JWT Bearer token. Returns None if not applicable."""
    from forum_memory.config import get_settings
    settings = get_settings()
    if not settings.jwt_enabled:
        return None

    if not authorization or not authorization.startswith("Bearer "):
        return None

    from forum_memory.core.auth import decode_access_token
    token = authorization[7:]  # Strip "Bearer "
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(401, "Token 无效或已过期，请重新登录")

    employee_id = payload.get("sub")
    if not employee_id:
        raise HTTPException(401, "Token 格式异常")

    stmt = select(User).where(User.employee_id == employee_id, User.is_active.is_(True))
    user = session.exec(stmt).first()
    if not user:
        raise HTTPException(401, f"Token 对应的工号 {employee_id} 未注册或已停用")
    return user


def _resolve_user_from_cookie(request: Request, session: Session) -> User | None:
    """Try to resolve user from SSO cookies.

    Reads hwsso_login/hwssot3/login_sid/login_uid cookies, verifies with
    external SSO API, and auto-provisions the user if not already registered.

    From user_info: uid → employee_id, displayNameCn → display_name, email → email.
    """
    from forum_memory.config import get_settings
    settings = get_settings()
    if not settings.sso_enabled:
        return None

    from forum_memory.core.auth import verify_sso_cookie
    user_info = verify_sso_cookie(dict(request.cookies))
    if not user_info:
        return None

    uid = user_info.get("uid", "").lower().strip()
    display_name = user_info.get("displayNameCn", "").strip()
    email = user_info.get("email", "")[0] if user_info.get("email", "") else "None"

    if not uid:
        logger.warning("SSO cookie verified but uid is empty")
        return None

    # Find existing user by employee_id
    stmt = select(User).where(User.employee_id == uid)
    user = session.exec(stmt).first()

    if user:
        # Update display_name and email if changed
        changed = False
        if display_name and user.display_name != display_name:
            user.display_name = display_name
            changed = True
        if email and user.email != email:
            user.email = email
            changed = True
        if not user.is_active:
            user.is_active = True
            changed = True
        if changed:
            session.commit()
            session.refresh(user)
        return user

    # Auto-provision new user from SSO info
    user = User(
        employee_id=uid,
        username=uid,
        display_name=display_name or uid,
        email=email or None,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    logger.info("Auto-provisioned user from SSO: employee_id=%s, display_name=%s", uid, display_name)
    return user


def get_current_user(
    request: Request,
    authorization: str = Header(default=""),
    token: str = Query(default=""),
    session: Session = Depends(get_db),
) -> User:
    """
    认证用户。支持三种方式（按优先级）：
    1. JWT: Authorization: Bearer <token>（jwt_enabled=True 时优先使用）
    2. JWT query param: ?token=<token>（用于 EventSource 等无法发送 Header 的场景）
    3. SSO Cookie: hwsso_login 等 cookie（默认认证方式）
    """
    # 1. Try JWT header first (if enabled)
    jwt_user = _resolve_user_from_jwt(authorization, session)
    if jwt_user:
        return jwt_user

    # 2. Try JWT query param (for EventSource / SSE)
    if token:
        qs_user = _resolve_user_from_jwt(f"Bearer {token}", session)
        if qs_user:
            return qs_user

    # 3. SSO cookie (default)
    cookie_user = _resolve_user_from_cookie(request, session)
    if cookie_user:
        return cookie_user

    raise HTTPException(401, "缺少认证信息，请登录后重试")


def get_current_user_id(user: User = Depends(get_current_user)) -> UUID:
    """提取当前用户的 UUID（向后兼容）。"""
    return user.id


def require_admin(user: User = Depends(get_current_user)) -> User:
    """要求当前用户是超级管理员，否则 403。"""
    if user.role != SystemRole.SUPER_ADMIN:
        raise HTTPException(403, "需要超级管理员权限")
    return user


def require_any_admin(user: User = Depends(get_current_user)) -> User:
    """要求当前用户是超级管理员或板块管理员，否则 403。"""
    if user.role not in (SystemRole.SUPER_ADMIN, SystemRole.BOARD_ADMIN):
        raise HTTPException(403, "需要管理员权限")
    return user


def get_managed_namespace_ids(session: Session, user: User) -> list[UUID]:
    """返回 board_admin 管理的板块 ID 列表。super_admin 返回空列表（表示不受限）。"""
    if user.role == SystemRole.SUPER_ADMIN:
        return []
    stmt = select(NamespaceModerator.namespace_id).where(
        NamespaceModerator.user_id == user.id,
        NamespaceModerator.role == MemberRole.MODERATOR,
    )
    return list(session.exec(stmt).all())


def check_board_permission(
    ns_id: UUID,
    session: Session,
    user: User,
) -> None:
    """检查用户是否有板块管理权限（超级管理员、owner 或 moderator 角色）。"""
    if user.role == SystemRole.SUPER_ADMIN:
        return
    from forum_memory.models.namespace import Namespace
    ns = session.get(Namespace, ns_id)
    if ns and ns.owner_id == user.id:
        return
    stmt = select(NamespaceModerator).where(
        NamespaceModerator.user_id == user.id,
        NamespaceModerator.namespace_id == ns_id,
        NamespaceModerator.role == MemberRole.MODERATOR,
    )
    if session.exec(stmt).first():
        return
    raise HTTPException(403, "需要板块管理权限")


def _is_namespace_member(ns_id: UUID, session: Session, user: User) -> bool:
    """Check if user is a member of the namespace (any role: owner, moderator, member)."""
    from forum_memory.models.namespace import Namespace
    if user.role == SystemRole.SUPER_ADMIN:
        return True
    ns = session.get(Namespace, ns_id)
    if ns and ns.owner_id == user.id:
        return True
    stmt = select(NamespaceModerator).where(
        NamespaceModerator.user_id == user.id,
        NamespaceModerator.namespace_id == ns_id,
    )
    return session.exec(stmt).first() is not None


def check_namespace_read_access(
    ns_id: UUID,
    session: Session,
    user: User,
) -> None:
    """Check read access. PRIVATE boards require membership."""
    from forum_memory.models.namespace import Namespace
    ns = session.get(Namespace, ns_id)
    if not ns:
        raise HTTPException(404, "Namespace not found")
    if ns.access_mode == AccessMode.PRIVATE:
        if not _is_namespace_member(ns_id, session, user):
            raise HTTPException(403, "该板块仅成员可访问")


def check_namespace_write_access(
    ns_id: UUID,
    session: Session,
    user: User,
) -> None:
    """Check write access. PRIVATE boards require membership."""
    from forum_memory.models.namespace import Namespace
    ns = session.get(Namespace, ns_id)
    if not ns:
        raise HTTPException(404, "Namespace not found")
    if ns.access_mode == AccessMode.PRIVATE:
        if not _is_namespace_member(ns_id, session, user):
            raise HTTPException(403, "该板块仅成员可发帖")
