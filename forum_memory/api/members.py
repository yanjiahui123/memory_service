"""Namespace membership and invite API routes — sync."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from forum_memory.api.deps import get_db, get_current_user, check_board_permission
from forum_memory.models.user import User
from forum_memory.schemas.membership import (
    MemberAdd, MemberBatchAdd, MemberBatchByDept,
    MemberRead, MemberRoleUpdate,
    InviteCreate, InviteRead,
)
from forum_memory.services import membership_service

router = APIRouter(tags=["members"])


# ── Member management ────────────────────────────────────────

@router.get(
    "/namespaces/{ns_id}/members",
    response_model=list[MemberRead],
)
def list_members(
    ns_id: UUID,
    role: str | None = None,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """列出板块所有成员（含角色）。需要板块管理权限。"""
    check_board_permission(ns_id, session, user)
    return membership_service.list_members(session, ns_id, role_filter=role)


@router.post(
    "/namespaces/{ns_id}/members",
    response_model=MemberRead,
    status_code=201,
)
def add_member(
    ns_id: UUID,
    data: MemberAdd,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """添加单个成员。通过工号查找，自动从外部接口获取用户信息。"""
    check_board_permission(ns_id, session, user)
    employee_id = data.employee_id.strip()
    if not employee_id:
        raise HTTPException(400, "工号不能为空")
    return membership_service.add_member(session, ns_id, employee_id, data.role)


@router.post("/namespaces/{ns_id}/members/batch")
def batch_add_members(
    ns_id: UUID,
    data: MemberBatchAdd,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """批量添加成员（工号列表，最多 100 个）。"""
    check_board_permission(ns_id, session, user)
    return membership_service.batch_add_members(
        session, ns_id, data.employee_ids, data.role,
    )


@router.post("/namespaces/{ns_id}/members/batch-by-dept")
def batch_add_by_department(
    ns_id: UUID,
    data: MemberBatchByDept,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """按部门批量添加成员。调用外部接口获取部门下所有人员。"""
    check_board_permission(ns_id, session, user)
    return membership_service.batch_add_by_department(
        session, ns_id, data.dept_code, data.role,
    )


@router.put("/namespaces/{ns_id}/members/{user_id}/role")
def update_member_role(
    ns_id: UUID,
    user_id: UUID,
    data: MemberRoleUpdate,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """变更成员角色（moderator ↔ member）。"""
    check_board_permission(ns_id, session, user)
    try:
        mem = membership_service.update_member_role(
            session, ns_id, user_id, data.role,
        )
        return {"user_id": str(user_id), "role": mem.role}
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@router.delete("/namespaces/{ns_id}/members/{user_id}", status_code=204)
def remove_member(
    ns_id: UUID,
    user_id: UUID,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """移除成员。"""
    check_board_permission(ns_id, session, user)
    try:
        membership_service.remove_member(session, ns_id, user_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


# ── Invite management ────────────────────────────────────────

@router.post(
    "/namespaces/{ns_id}/invites",
    response_model=InviteRead,
    status_code=201,
)
def create_invite(
    ns_id: UUID,
    data: InviteCreate,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """生成邀请链接。"""
    check_board_permission(ns_id, session, user)
    invite = membership_service.create_invite(
        session, ns_id, user.id, data.role, data.max_uses, data.expires_hours,
    )
    return invite


@router.get(
    "/namespaces/{ns_id}/invites",
    response_model=list[InviteRead],
)
def list_invites(
    ns_id: UUID,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查看有效邀请列表。"""
    check_board_permission(ns_id, session, user)
    return membership_service.list_invites(session, ns_id)


@router.delete("/namespaces/{ns_id}/invites/{invite_id}", status_code=204)
def revoke_invite(
    ns_id: UUID,
    invite_id: UUID,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """撤销邀请链接。"""
    check_board_permission(ns_id, session, user)
    try:
        membership_service.revoke_invite(session, invite_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


# ── Join via invite (top-level route) ────────────────────────

@router.get("/invites/{code}")
def get_invite_info(
    code: str,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查看邀请信息（板块名称等）。"""
    invite = membership_service.get_invite_by_code(session, code)
    if not invite:
        raise HTTPException(404, "邀请链接无效")
    from forum_memory.models.namespace import Namespace
    ns = session.get(Namespace, invite.namespace_id)
    return {
        "namespace_id": str(invite.namespace_id),
        "namespace_display_name": ns.display_name if ns else "",
        "role": invite.role,
        "expires_at": invite.expires_at.isoformat() if invite.expires_at else None,
    }


@router.post("/invites/{code}/join")
def join_via_invite(
    code: str,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """通过邀请码加入板块。"""
    try:
        return membership_service.join_via_invite(session, code, user)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
