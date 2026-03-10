"""User API routes — sync."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from forum_memory.api.deps import get_db, get_current_user, require_admin
from forum_memory.models.user import User
from forum_memory.models.namespace import Namespace
from forum_memory.models.namespace_moderator import NamespaceModerator
from forum_memory.models.enums import SystemRole
from forum_memory.schemas.user import UserCreate, UserRead
from forum_memory.schemas.namespace import NamespaceRead

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
        stmt = select(Namespace).where(Namespace.is_active == True)
        return list(session.exec(stmt).all())
    stmt = (
        select(Namespace)
        .join(NamespaceModerator, NamespaceModerator.namespace_id == Namespace.id)
        .where(NamespaceModerator.user_id == user.id)
        .where(Namespace.is_active == True)
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


@router.delete("/{user_id}", status_code=204)
def deactivate_user(user_id: UUID, session: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """管理员：停用用户。"""
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    if user.employee_id == "00000000":
        raise HTTPException(400, "不能停用超级管理员")
    user.is_active = False
    session.commit()
