"""FastAPI dependencies — sync session, user lookup, and access control.

Authentication strategy:
- When jwt_enabled=True: Accepts Authorization: Bearer <token> (preferred)
  and falls back to X-Employee-Id header for backward compatibility.
- When jwt_enabled=False (default): Only X-Employee-Id header is used.
"""

from uuid import UUID

from fastapi import Depends, Header, HTTPException
from sqlmodel import Session, select

from forum_memory.database import get_session
from forum_memory.models.user import User
from forum_memory.models.namespace_moderator import NamespaceModerator
from forum_memory.models.enums import SystemRole


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

    stmt = select(User).where(User.employee_id == employee_id, User.is_active == True)
    user = session.exec(stmt).first()
    if not user:
        raise HTTPException(401, f"Token 对应的工号 {employee_id} 未注册或已停用")
    return user


def get_current_user(
    x_employee_id: str = Header(default=""),
    authorization: str = Header(default=""),
    session: Session = Depends(get_db),
) -> User:
    """
    认证用户。支持两种方式：
    1. JWT: Authorization: Bearer <token>（jwt_enabled=True 时优先使用）
    2. 工号: X-Employee-Id 请求头（向后兼容）
    """
    # Try JWT first (if enabled)
    jwt_user = _resolve_user_from_jwt(authorization, session)
    if jwt_user:
        return jwt_user

    # Fall back to X-Employee-Id
    employee_id = x_employee_id.strip()
    if not employee_id:
        from forum_memory.config import get_settings
        if get_settings().jwt_enabled:
            raise HTTPException(401, "缺少认证信息：请提供 Authorization: Bearer <token> 或 X-Employee-Id 请求头")
        raise HTTPException(401, "缺少 X-Employee-Id 请求头，请设置你的工号")

    stmt = select(User).where(User.employee_id == employee_id, User.is_active == True)
    user = session.exec(stmt).first()
    if not user:
        raise HTTPException(401, f"工号 {employee_id} 未注册，请联系管理员")
    return user


def get_current_user_id(user: User = Depends(get_current_user)) -> UUID:
    """提取当前用户的 UUID（向后兼容）。"""
    return user.id


def require_admin(user: User = Depends(get_current_user)) -> User:
    """要求当前用户是超级管理员，否则 403。"""
    if user.role != SystemRole.SUPER_ADMIN:
        raise HTTPException(403, "需要超级管理员权限")
    return user


def check_board_permission(
    ns_id: UUID,
    session: Session,
    user: User,
) -> None:
    """检查用户是否有板块管理权限（超级管理员或该板块的管理员）。"""
    if user.role == SystemRole.SUPER_ADMIN:
        return
    if user.role == SystemRole.BOARD_ADMIN:
        stmt = select(NamespaceModerator).where(
            NamespaceModerator.user_id == user.id,
            NamespaceModerator.namespace_id == ns_id,
        )
        if session.exec(stmt).first():
            return
    raise HTTPException(403, "需要板块管理权限")


def _is_namespace_member(ns_id: UUID, session: Session, user: User) -> bool:
    """Check if user is a member of the namespace (owner, moderator, or super_admin)."""
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
    """Check if user can read content in this namespace.
    PRIVATE: only members can read. PUBLIC/RESTRICTED: anyone can read."""
    from forum_memory.models.namespace import Namespace
    ns = session.get(Namespace, ns_id)
    if not ns:
        raise HTTPException(404, "Namespace not found")
    if ns.access_mode != "private":
        return  # PUBLIC and RESTRICTED allow reading by anyone
    if not _is_namespace_member(ns_id, session, user):
        raise HTTPException(403, "此板块为私有板块，仅成员可访问")


def check_namespace_write_access(
    ns_id: UUID,
    session: Session,
    user: User,
) -> None:
    """Check if user can post/create content in this namespace.
    RESTRICTED/PRIVATE: only members can write. PUBLIC: anyone can write."""
    from forum_memory.models.namespace import Namespace
    ns = session.get(Namespace, ns_id)
    if not ns:
        raise HTTPException(404, "Namespace not found")
    if ns.access_mode == "public":
        return  # PUBLIC allows writing by anyone
    if not _is_namespace_member(ns_id, session, user):
        raise HTTPException(403, "此板块仅成员可发帖")
