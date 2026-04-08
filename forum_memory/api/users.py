"""User API routes — sync."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from forum_memory.api.deps import get_db, get_current_user, require_admin, require_any_admin
from forum_memory.models.user import User
from forum_memory.models.namespace import Namespace
from forum_memory.models.namespace_moderator import NamespaceModerator
from forum_memory.models.board_follow import BoardFollow
from forum_memory.models.enums import SystemRole, MemberRole
from forum_memory.schemas.user import UserCreate, UserUpdate, UserRead
from forum_memory.schemas.namespace import NamespaceRead
from forum_memory.services import user_directory_service

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserRead)
def get_me(user: User = Depends(get_current_user)):
    """获取当前登录用户信息。"""
    return user


@router.get("/me/managed-namespaces", response_model=list[NamespaceRead])
def get_my_managed_namespaces(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """返回当前用户管理的板块列表。超级管理员返回所有板块，板块管理员只返回自己管理的板块。"""
    if user.role == SystemRole.SUPER_ADMIN:
        stmt = select(Namespace).where(Namespace.is_active.is_(True))
        return list(session.exec(stmt).all())
    stmt = (
        select(Namespace)
        .join(NamespaceModerator, NamespaceModerator.namespace_id == Namespace.id)
        .where(NamespaceModerator.user_id == user.id)
        .where(NamespaceModerator.role == MemberRole.MODERATOR)
        .where(Namespace.is_active.is_(True))
    )
    return list(session.exec(stmt).all())


@router.get("/me/followed-boards", response_model=list[NamespaceRead])
def get_my_followed_boards(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """返回当前用户关注的板块列表。"""
    stmt = (
        select(Namespace)
        .join(BoardFollow, BoardFollow.namespace_id == Namespace.id)
        .where(BoardFollow.user_id == user.id)
        .where(Namespace.is_active.is_(True))
        .order_by(BoardFollow.created_at)
    )
    return list(session.exec(stmt).all())


@router.get("", response_model=list[UserRead])
def list_users(session: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """管理员：查看所有用户。"""
    stmt = select(User).order_by(User.employee_id)
    return list(session.exec(stmt).all())


@router.post("", response_model=UserRead, status_code=201)
def create_user(data: UserCreate, session: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """管理员：注册新用户。"""
    # 检查工号是否已存在
    existing = session.exec(select(User).where(User.employee_id == data.employee_id)).first()
    if existing:
        raise HTTPException(409, f"工号 {data.employee_id} 已存在")

    user = User(
        employee_id=data.employee_id,
        username=data.username,
        display_name=data.display_name,
        email=data.email,
        role=SystemRole(data.role),
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@router.put("/{user_id}", response_model=UserRead)
def update_user(
    user_id: UUID,
    data: UserUpdate,
    session: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """管理员：修改用户信息或角色。"""
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    if user.employee_id == "00000000" and data.role and data.role != "super_admin":
        raise HTTPException(400, "不能修改系统管理员的角色")
    _apply_user_updates(user, data)
    session.commit()
    session.refresh(user)
    return user


def _apply_user_updates(user: User, data: UserUpdate) -> None:
    if data.display_name is not None:
        user.display_name = data.display_name
    if data.username is not None:
        user.username = data.username
    if data.email is not None:
        user.email = data.email
    if data.role is not None:
        user.role = SystemRole(data.role)


# ── User search & department listing ─────────────────────────

@router.get("/search")
def search_users_api(
    q: str,
    admin: User = Depends(require_any_admin),
):
    """模糊搜索外部用户目录，返回候选列表。"""
    if not q.strip():
        return []
    return user_directory_service.search_users(q.strip(), page_size=20)


@router.get("/departments")
def list_departments(
    session: Session = Depends(get_db),
    admin: User = Depends(require_any_admin),
):
    """返回本地数据库中所有已知部门（去重）。"""
    stmt = (
        select(User.dept_code, User.dept_path)
        .where(User.dept_code.isnot(None))
        .where(User.is_active.is_(True))
        .distinct()
        .order_by(User.dept_path)
    )
    rows = session.exec(stmt).all()
    return [{"dept_code": code, "dept_path": path} for code, path in rows]
