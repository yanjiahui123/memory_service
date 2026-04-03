"""Namespace (board) API routes — sync."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, func, select

from forum_memory.api.deps import get_db, get_current_user, check_board_permission, check_namespace_read_access
from forum_memory.models.thread import Thread
from forum_memory.models.enums import ThreadStatus
from forum_memory.models.user import User
from forum_memory.models.namespace_moderator import NamespaceModerator
from forum_memory.models.board_follow import BoardFollow
from forum_memory.models.enums import SystemRole
from forum_memory.schemas.namespace import NamespaceCreate, NamespaceUpdate, NamespaceRead, NamespaceStats, DictionaryUpdate
from forum_memory.schemas.user import UserRead
from forum_memory.services import namespace_service

router = APIRouter(prefix="/namespaces", tags=["namespaces"])


class ModeratorAdd(BaseModel):
    employee_id: str
    display_name: str | None = None  # 用户不存在时用于自动创建


@router.get("", response_model=list[NamespaceRead])
def list_namespaces(
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查看板块列表（PRIVATE 板块仅成员可见）。"""
    namespaces = namespace_service.list_namespaces(session, user)
    ns_ids = [ns.id for ns in namespaces]
    total_counts: dict = {}
    open_counts: dict = {}
    if ns_ids:
        for ns_id, cnt in session.exec(
            select(Thread.namespace_id, func.count()).select_from(Thread)
            .where(Thread.namespace_id.in_(ns_ids), Thread.status != ThreadStatus.DELETED)
            .group_by(Thread.namespace_id)
        ).all():
            total_counts[ns_id] = cnt
        for ns_id, cnt in session.exec(
            select(Thread.namespace_id, func.count()).select_from(Thread)
            .where(Thread.namespace_id.in_(ns_ids), Thread.status == ThreadStatus.OPEN)
            .group_by(Thread.namespace_id)
        ).all():
            open_counts[ns_id] = cnt
    result = []
    for ns in namespaces:
        d = NamespaceRead.model_validate(ns).model_dump()
        d["thread_count"] = total_counts.get(ns.id, 0)
        d["open_thread_count"] = open_counts.get(ns.id, 0)
        result.append(d)
    return result


@router.get("/stats/aggregate", response_model=NamespaceStats)
def get_aggregate_stats(session: Session = Depends(get_db)):
    """聚合所有板块的统计数据。"""
    return namespace_service.get_aggregate_stats(session)


@router.get("/{ns_id}", response_model=NamespaceRead)
def get_namespace(
    ns_id: UUID,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查看板块详情（PRIVATE 板块仅成员可见）。"""
    ns = namespace_service.get_namespace(session, ns_id)
    if not ns:
        raise HTTPException(404, "Namespace not found")
    check_namespace_read_access(ns_id, session, user)
    return ns


@router.post("", response_model=NamespaceRead, status_code=201)
def create_namespace(
    data: NamespaceCreate,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """超级管理员或板块管理员可创建板块。板块管理员创建后自动成为该板块的管理员。"""
    if user.role not in (SystemRole.SUPER_ADMIN, SystemRole.BOARD_ADMIN):
        raise HTTPException(403, "需要管理员权限才能创建板块")
    add_as_mod = user.role == SystemRole.BOARD_ADMIN
    return namespace_service.create_namespace(session, data, user.id, add_as_moderator=add_as_mod)


@router.put("/{ns_id}", response_model=NamespaceRead)
def update_namespace(
    ns_id: UUID,
    data: NamespaceUpdate,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """超级管理员或板块管理员可修改板块。"""
    check_board_permission(ns_id, session, user)
    ns = namespace_service.update_namespace(session, ns_id, data)
    if not ns:
        raise HTTPException(404, "Namespace not found")
    return ns


@router.delete("/{ns_id}", response_model=NamespaceRead)
def delete_namespace(
    ns_id: UUID,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """超级管理员或该板块管理员可删除板块（软删除）。"""
    check_board_permission(ns_id, session, user)
    try:
        return namespace_service.delete_namespace(session, ns_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@router.get("/{ns_id}/stats", response_model=NamespaceStats)
def get_stats(ns_id: UUID, session: Session = Depends(get_db)):
    return namespace_service.get_stats(session, ns_id)


@router.put("/{ns_id}/dictionary", response_model=NamespaceRead)
def update_dictionary(
    ns_id: UUID,
    data: DictionaryUpdate,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """超级管理员或板块管理员可修改黑话字典。"""
    check_board_permission(ns_id, session, user)
    ns = namespace_service.update_dictionary(session, ns_id, data.entries)
    if not ns:
        raise HTTPException(404, "Namespace not found")
    return ns


# ── Moderator management ──────────────────────────────────────

@router.get("/{ns_id}/moderators", response_model=list[UserRead])
def list_moderators(
    ns_id: UUID,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查看板块管理员列表（超级管理员或该板块管理员可查看）。"""
    check_board_permission(ns_id, session, user)
    stmt = (
        select(User)
        .join(NamespaceModerator, NamespaceModerator.user_id == User.id)
        .where(
            NamespaceModerator.namespace_id == ns_id,
            NamespaceModerator.role == "moderator",
        )
    )
    return list(session.exec(stmt).all())


@router.post("/{ns_id}/moderators", response_model=UserRead, status_code=201)
def add_moderator(
    ns_id: UUID,
    data: ModeratorAdd,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """超级管理员或该板块管理员可指派板块管理员。通过工号查找用户，不存在则自动创建。"""
    check_board_permission(ns_id, session, user)
    ns = namespace_service.get_namespace(session, ns_id)
    if not ns:
        raise HTTPException(404, "板块不存在")

    employee_id = data.employee_id.strip()
    if not employee_id:
        raise HTTPException(400, "工号不能为空")

    # 通过工号查找用户，不存在则自动创建
    target_user = session.exec(
        select(User).where(User.employee_id == employee_id)
    ).first()

    if target_user and not target_user.is_active:
        raise HTTPException(400, f"工号 {employee_id} 已停用")

    if not target_user:
        display = data.display_name or employee_id
        target_user = User(
            employee_id=employee_id,
            username=employee_id,
            display_name=display,
            role=SystemRole.BOARD_ADMIN,
        )
        session.add(target_user)
        session.flush()

    if target_user.role == SystemRole.SUPER_ADMIN:
        raise HTTPException(400, "超级管理员无需指派为板块管理员")

    # Check duplicate
    existing = session.exec(
        select(NamespaceModerator).where(
            NamespaceModerator.user_id == target_user.id,
            NamespaceModerator.namespace_id == ns_id,
        )
    ).first()
    if existing:
        raise HTTPException(409, "该用户已是此板块管理员")

    # Update user role to board_admin if currently a regular user
    if target_user.role == SystemRole.USER:
        target_user.role = SystemRole.BOARD_ADMIN
        session.add(target_user)

    mod = NamespaceModerator(user_id=target_user.id, namespace_id=ns_id)
    session.add(mod)
    session.commit()
    session.refresh(target_user)
    return target_user


@router.delete("/{ns_id}/moderators/{user_id}", status_code=204)
def remove_moderator(
    ns_id: UUID,
    user_id: UUID,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """超级管理员或该板块管理员可移除板块管理员。"""
    check_board_permission(ns_id, session, user)
    stmt = select(NamespaceModerator).where(
        NamespaceModerator.user_id == user_id,
        NamespaceModerator.namespace_id == ns_id,
    )
    mod = session.exec(stmt).first()
    if not mod:
        raise HTTPException(404, "未找到该管理员分配记录")
    session.delete(mod)
    session.commit()

    # If user has no more moderator assignments, revert role to USER
    remaining = session.exec(
        select(NamespaceModerator).where(NamespaceModerator.user_id == user_id)
    ).first()
    if not remaining:
        target_user = session.get(User, user_id)
        if target_user and target_user.role == SystemRole.BOARD_ADMIN:
            target_user.role = SystemRole.USER
            session.commit()


# ── Board follow/subscribe ───────────────────────────────────

@router.post("/{ns_id}/follow", status_code=201)
def follow_board(
    ns_id: UUID,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """关注板块。"""
    ns = namespace_service.get_namespace(session, ns_id)
    if not ns:
        raise HTTPException(404, "板块不存在")
    existing = session.exec(
        select(BoardFollow).where(
            BoardFollow.user_id == user.id,
            BoardFollow.namespace_id == ns_id,
        )
    ).first()
    if existing:
        return {"followed": True}
    follow = BoardFollow(user_id=user.id, namespace_id=ns_id)
    session.add(follow)
    session.commit()
    return {"followed": True}


@router.delete("/{ns_id}/follow", status_code=200)
def unfollow_board(
    ns_id: UUID,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """取消关注板块。"""
    existing = session.exec(
        select(BoardFollow).where(
            BoardFollow.user_id == user.id,
            BoardFollow.namespace_id == ns_id,
        )
    ).first()
    if existing:
        session.delete(existing)
        session.commit()
    return {"followed": False}


@router.get("/{ns_id}/follow", response_model=dict)
def check_follow_status(
    ns_id: UUID,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """检查是否已关注板块。"""
    existing = session.exec(
        select(BoardFollow).where(
            BoardFollow.user_id == user.id,
            BoardFollow.namespace_id == ns_id,
        )
    ).first()
    return {"followed": existing is not None}
