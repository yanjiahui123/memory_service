"""Board share link API routes — sync."""

import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from forum_memory.api.deps import get_current_user, get_db
from forum_memory.models.board_share_link import BoardShareLink, BoardShareLinkNamespace
from forum_memory.models.enums import SystemRole
from forum_memory.models.namespace import Namespace
from forum_memory.models.user import User
from forum_memory.schemas.share_link import (
    ShareLinkCreate,
    ShareLinkInfo,
    ShareLinkRead,
)
from forum_memory.services.membership_service import _auto_follow, _upsert_membership

router = APIRouter(tags=["share-links"])


# ── Helpers ─────────────────────────────────────────────────

def _require_super_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != SystemRole.SUPER_ADMIN:
        raise HTTPException(403, "仅超级管理员可执行此操作")
    return user


def _build_share_link_read(
    session: Session,
    link: BoardShareLink,
) -> dict:
    """Build ShareLinkRead dict with namespace display names."""
    junctions = list(session.exec(
        select(BoardShareLinkNamespace).where(
            BoardShareLinkNamespace.share_link_id == link.id,
        )
    ).all())
    ns_map = _load_ns_map(session, [j.namespace_id for j in junctions])
    ns_infos = [
        {"namespace_id": str(ns_map[j.namespace_id].id),
         "display_name": ns_map[j.namespace_id].display_name}
        for j in junctions if j.namespace_id in ns_map
    ]
    return {
        "id": link.id,
        "code": link.code,
        "name": link.name,
        "use_count": link.use_count,
        "is_active": link.is_active,
        "created_at": link.created_at,
        "namespaces": ns_infos,
    }


def _load_ns_map(session: Session, ns_ids: list) -> dict:
    """Batch-load Namespaces by id list, return {id: Namespace}."""
    if not ns_ids:
        return {}
    rows = session.exec(select(Namespace).where(Namespace.id.in_(ns_ids))).all()
    return {ns.id: ns for ns in rows}


# ── Admin endpoints (super_admin only) ─────────────────────

@router.post("/share-links", response_model=ShareLinkRead, status_code=201)
def create_share_link(
    data: ShareLinkCreate,
    session: Session = Depends(get_db),
    user: User = Depends(_require_super_admin),
):
    """创建多板块分享链接。"""
    if not data.namespace_ids:
        raise HTTPException(400, "至少选择一个板块")

    # Validate all namespaces exist
    ns_uuids = [UUID(ns_id) for ns_id in data.namespace_ids]
    ns_map = _load_ns_map(session, ns_uuids)
    for ns_id, uid in zip(data.namespace_ids, ns_uuids):
        ns = ns_map.get(uid)
        if not ns or not ns.is_active:
            raise HTTPException(404, f"板块 {ns_id} 不存在或已删除")

    link = BoardShareLink(
        code=secrets.token_urlsafe(12),
        name=data.name.strip(),
        created_by=user.id,
    )
    session.add(link)
    session.flush()

    for ns_id in data.namespace_ids:
        junction = BoardShareLinkNamespace(
            share_link_id=link.id,
            namespace_id=UUID(ns_id),
        )
        session.add(junction)

    session.commit()
    session.refresh(link)
    return _build_share_link_read(session, link)


@router.get("/share-links", response_model=list[ShareLinkRead])
def list_share_links(
    session: Session = Depends(get_db),
    user: User = Depends(_require_super_admin),
):
    """列出所有分享链接（含已撤销）。"""
    links = session.exec(
        select(BoardShareLink).order_by(BoardShareLink.created_at.desc())
    ).all()
    return [_build_share_link_read(session, lk) for lk in links]


@router.delete("/share-links/{link_id}", status_code=204)
def revoke_share_link(
    link_id: UUID,
    session: Session = Depends(get_db),
    user: User = Depends(_require_super_admin),
):
    """撤销分享链接。"""
    link = session.get(BoardShareLink, link_id)
    if not link:
        raise HTTPException(404, "分享链接不存在")
    link.is_active = False
    session.commit()


# ── Public endpoints ────────────────────────────────────────

@router.get("/share-links/code/{code}", response_model=ShareLinkInfo)
def get_share_link_info(
    code: str,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查看分享链接信息（板块列表）。"""
    link = session.exec(
        select(BoardShareLink).where(BoardShareLink.code == code)
    ).first()
    if not link or not link.is_active:
        raise HTTPException(404, "分享链接无效或已撤销")

    junctions = list(session.exec(
        select(BoardShareLinkNamespace).where(
            BoardShareLinkNamespace.share_link_id == link.id,
        )
    ).all())
    ns_map = _load_ns_map(session, [j.namespace_id for j in junctions])
    ns_infos = [
        {"namespace_id": str(ns_map[j.namespace_id].id),
         "display_name": ns_map[j.namespace_id].display_name}
        for j in junctions
        if j.namespace_id in ns_map and ns_map[j.namespace_id].is_active
    ]

    return {"code": link.code, "name": link.name, "namespaces": ns_infos}


@router.post("/share-links/code/{code}/join")
def join_via_share_link(
    code: str,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """通过分享链接加入所有关联板块。"""
    link = session.exec(
        select(BoardShareLink).where(BoardShareLink.code == code)
    ).first()
    if not link or not link.is_active:
        raise HTTPException(400, "分享链接无效或已撤销")

    junctions = list(session.exec(
        select(BoardShareLinkNamespace).where(
            BoardShareLinkNamespace.share_link_id == link.id,
        )
    ).all())
    ns_map = _load_ns_map(session, [j.namespace_id for j in junctions])

    joined = []
    for j in junctions:
        ns = ns_map.get(j.namespace_id)
        if not ns or not ns.is_active:
            continue
        _upsert_membership(session, j.namespace_id, user.id, "member")
        _auto_follow(session, j.namespace_id, user.id)
        joined.append({
            "namespace_id": str(ns.id),
            "display_name": ns.display_name,
        })

    link.use_count += 1
    session.commit()

    return {"joined": joined, "count": len(joined)}
