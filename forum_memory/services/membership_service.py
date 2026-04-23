"""Membership management service — sync."""

import logging
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, select

from forum_memory.models.user import User
from forum_memory.models.namespace_moderator import NamespaceModerator
from forum_memory.models.namespace_invite import NamespaceInvite
from forum_memory.models.enums import SystemRole, MemberRole

logger = logging.getLogger(__name__)

_TZ8 = timezone(timedelta(hours=8))


# ── Member CRUD ──────────────────────────────────────────────

def list_members(
    session: Session,
    ns_id: UUID,
    role_filter: str | None = None,
) -> list[dict]:
    """List all members of a namespace with user info."""
    stmt = (
        select(User, NamespaceModerator)
        .join(NamespaceModerator, NamespaceModerator.user_id == User.id)
        .where(NamespaceModerator.namespace_id == ns_id)
    )
    if role_filter:
        stmt = stmt.where(NamespaceModerator.role == role_filter)
    rows = session.exec(stmt).all()
    return [_to_member_dict(user, mem) for user, mem in rows]


def _to_member_dict(user: User, mem: NamespaceModerator) -> dict:
    return {
        "user_id": user.id,
        "employee_id": user.employee_id,
        "display_name": user.display_name,
        "dept_path": user.dept_path,
        "role": mem.role,
        "joined_at": mem.created_at,
    }


def add_member(
    session: Session,
    ns_id: UUID,
    employee_id: str,
    role: str = "member",
) -> dict:
    """Add a single member by employee_id. Auto-provisions user if needed."""
    target = _find_or_create_user(session, employee_id)
    mem = _upsert_membership(session, ns_id, target.id, role)
    if role == MemberRole.MODERATOR:
        _sync_role_after_promote(session, target)
    session.commit()
    session.refresh(target)
    session.refresh(mem)
    return _to_member_dict(target, mem)


def _find_or_create_user(session: Session, employee_id: str) -> User:
    """Find user by employee_id or create with info from external directory.

    Raises ValueError if user not found locally and external lookup fails.
    """
    stmt = select(User).where(User.employee_id == employee_id)
    user = session.exec(stmt).first()
    if user:
        return user
    # Must verify via external directory before creating
    info = _lookup_external(employee_id)
    if not info:
        raise ValueError(f"用户 {employee_id} 不存在")
    user = User(
        employee_id=employee_id,
        username=employee_id,
        display_name=info.get("name", employee_id),
        email=info.get("email"),
        dept_code=info.get("dept_code"),
        dept_path=info.get("dept_path"),
        dept_levels=info.get("dept_levels"),
    )
    session.add(user)
    session.flush()
    return user


def _lookup_external(employee_id: str) -> dict | None:
    try:
        from forum_memory.services.user_directory_service import lookup_user
        return lookup_user(employee_id)
    except Exception:
        logger.warning("External lookup failed for %s", employee_id)
        return None


def _upsert_membership(
    session: Session, ns_id: UUID, user_id: UUID, role: str,
) -> NamespaceModerator:
    """Create membership or return existing one. Auto-follows the board."""
    stmt = select(NamespaceModerator).where(
        NamespaceModerator.user_id == user_id,
        NamespaceModerator.namespace_id == ns_id,
    )
    existing = session.exec(stmt).first()
    if existing:
        _auto_follow(session, ns_id, user_id)
        return existing
    mem = NamespaceModerator(user_id=user_id, namespace_id=ns_id, role=role)
    session.add(mem)
    _auto_follow(session, ns_id, user_id)
    session.flush()
    return mem


def batch_add_members(
    session: Session,
    ns_id: UUID,
    employee_ids: list[str],
    role: str = "member",
    max_count: int | None = 100,
) -> dict:
    """Batch add members. One SELECT + bulk INSERT per table, one commit."""
    ids = [eid.strip() for eid in employee_ids if eid.strip()]
    if max_count is not None:
        ids = ids[:max_count]
    if not ids:
        return {"added": 0, "skipped": 0, "errors": []}

    now = datetime.now(tz=_TZ8)
    existing_map = _bulk_fetch_users(session, ids)
    new_rows, update_rows, errors = _resolve_users_from_directory(ids, existing_map, now)
    all_users = _bulk_upsert_users(session, existing_map, new_rows, update_rows)

    if not all_users:
        session.commit()
        return {"added": 0, "skipped": 0, "errors": errors}

    added, skipped = _bulk_upsert_memberships(session, ns_id, all_users, role, now)

    if role == MemberRole.MODERATOR:
        for u in all_users.values():
            _sync_role_after_promote(session, u)

    session.commit()
    return {"added": added, "skipped": skipped, "errors": errors}


def _bulk_fetch_users(session: Session, ids: list[str]) -> dict[str, User]:
    """Fetch all users matching the given employee_ids in one query."""
    rows = session.exec(select(User).where(User.employee_id.in_(ids))).all()
    return {u.employee_id: u for u in rows}


_LOOKUP_WORKERS = 20


def _resolve_users_from_directory(
    ids: list[str],
    existing_map: dict[str, User],
    now: datetime,
) -> tuple[list[dict], list[dict], list[str]]:
    """Call external directory concurrently; split into new/update rows and errors."""
    with ThreadPoolExecutor(max_workers=_LOOKUP_WORKERS) as executor:
        infos = list(executor.map(_lookup_external, ids))
    lookup_results = dict(zip(ids, infos))

    new_rows: list[dict] = []
    update_rows: list[dict] = []
    errors: list[str] = []
    for eid in ids:
        info = lookup_results.get(eid)
        if not info:
            if eid not in existing_map:
                errors.append(f"{eid}: 用户不存在")
            continue
        row = {
            "employee_id": eid,
            "username": eid,
            "display_name": info.get("name") or eid,
            "email": info.get("email"),
            "dept_code": info.get("dept_code"),
            "dept_path": info.get("dept_path"),
            "dept_levels": info.get("dept_levels"),
        }
        if eid in existing_map:
            update_rows.append(row)
        else:
            new_rows.append({**row, "id": uuid4(), "created_at": now, "updated_at": now})
    return new_rows, update_rows, errors


def _bulk_upsert_users(
    session: Session,
    existing_map: dict[str, User],
    new_rows: list[dict],
    update_rows: list[dict],
) -> dict[str, User]:
    """Bulk-insert new users and update stale fields on existing ones, then re-fetch."""
    if new_rows:
        stmt = pg_insert(User).values(new_rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["employee_id"],
            set_={
                "display_name": stmt.excluded.display_name,
                "email": stmt.excluded.email,
                "dept_code": stmt.excluded.dept_code,
                "dept_path": stmt.excluded.dept_path,
                "dept_levels": stmt.excluded.dept_levels,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        session.exec(stmt)
    for row in update_rows:
        user = existing_map[row["employee_id"]]
        user.display_name = row["display_name"]
        user.email = row["email"]
        user.dept_code = row["dept_code"]
        user.dept_path = row["dept_path"]
        user.dept_levels = row["dept_levels"]
    session.flush()
    valid_eids = [r["employee_id"] for r in new_rows + update_rows] + [
        eid for eid in existing_map if eid not in {r["employee_id"] for r in update_rows}
    ]
    rows = session.exec(select(User).where(User.employee_id.in_(valid_eids))).all()
    return {u.employee_id: u for u in rows}


def _bulk_upsert_memberships(
    session: Session,
    ns_id: UUID,
    all_users: dict[str, User],
    role: str,
    now: datetime,
) -> tuple[int, int]:
    """Bulk-insert memberships and board follows; return (added, skipped)."""
    from forum_memory.models.board_follow import BoardFollow

    user_id_list = [u.id for u in all_users.values()]
    existing_members = session.exec(
        select(NamespaceModerator).where(
            NamespaceModerator.namespace_id == ns_id,
            NamespaceModerator.user_id.in_(user_id_list),
        )
    ).all()
    existing_member_ids: set[UUID] = {m.user_id for m in existing_members}
    new_mem_rows = [
        {"id": uuid4(), "user_id": u.id, "namespace_id": ns_id,
         "role": role, "created_at": now, "updated_at": now}
        for u in all_users.values() if u.id not in existing_member_ids
    ]
    if new_mem_rows:
        mem_stmt = pg_insert(NamespaceModerator).values(new_mem_rows)
        session.exec(mem_stmt.on_conflict_do_nothing(index_elements=["user_id", "namespace_id"]))
    follow_rows = [
        {"id": uuid4(), "user_id": u.id, "namespace_id": ns_id,
         "created_at": now, "updated_at": now}
        for u in all_users.values()
    ]
    follow_stmt = pg_insert(BoardFollow).values(follow_rows)
    session.exec(follow_stmt.on_conflict_do_nothing(index_elements=["user_id", "namespace_id"]))
    return len(new_mem_rows), len(all_users) - len(new_mem_rows)


def batch_add_by_department(
    session: Session,
    ns_id: UUID,
    dept_code: str,
    role: str = "member",
) -> dict:
    """Batch add all members from a department via external API."""
    from forum_memory.services.user_directory_service import list_dept_members
    dept_members = list_dept_members(dept_code)
    employee_ids = [m["w3account"] for m in dept_members if m.get("w3account")]
    result = batch_add_members(session, ns_id, employee_ids, role, max_count=None)
    result["total_in_dept"] = len(dept_members)
    return result


def update_member_role(
    session: Session,
    ns_id: UUID,
    user_id: UUID,
    new_role: str,
) -> NamespaceModerator:
    """Change a member's role."""
    stmt = select(NamespaceModerator).where(
        NamespaceModerator.user_id == user_id,
        NamespaceModerator.namespace_id == ns_id,
    )
    mem = session.exec(stmt).first()
    if not mem:
        raise ValueError("该用户不是此板块成员")
    old_role = mem.role
    mem.role = new_role
    user = session.get(User, user_id)
    if new_role == MemberRole.MODERATOR and old_role != MemberRole.MODERATOR:
        _sync_role_after_promote(session, user)
    elif new_role == MemberRole.MEMBER and old_role == MemberRole.MODERATOR:
        _sync_role_after_demote(session, user)
    session.commit()
    session.refresh(mem)
    return mem


def remove_member(session: Session, ns_id: UUID, user_id: UUID) -> None:
    """Remove a member from a namespace."""
    stmt = select(NamespaceModerator).where(
        NamespaceModerator.user_id == user_id,
        NamespaceModerator.namespace_id == ns_id,
    )
    mem = session.exec(stmt).first()
    if not mem:
        raise ValueError("该用户不是此板块成员")
    was_moderator = mem.role == MemberRole.MODERATOR
    session.delete(mem)
    session.commit()
    if was_moderator:
        user = session.get(User, user_id)
        _sync_role_after_demote(session, user)


def batch_remove_members(
    session: Session,
    ns_id: UUID,
    user_ids: list[UUID],
) -> dict:
    """Batch remove members. One SELECT, one DELETE, one commit."""
    if not user_ids:
        return {"removed": 0, "errors": []}
    members = session.exec(
        select(NamespaceModerator).where(
            NamespaceModerator.namespace_id == ns_id,
            NamespaceModerator.user_id.in_(user_ids),
        )
    ).all()
    found_ids = {m.user_id for m in members}
    errors = [f"{uid}: 不是此板块成员" for uid in user_ids if uid not in found_ids]
    ex_moderator_ids = [m.user_id for m in members if m.role == MemberRole.MODERATOR]
    if not found_ids:
        return {"removed": 0, "errors": errors}
    session.exec(
        delete(NamespaceModerator).where(
            NamespaceModerator.namespace_id == ns_id,
            NamespaceModerator.user_id.in_(found_ids),
        )
    )
    session.commit()
    if ex_moderator_ids:
        _batch_sync_role_after_demote(session, ex_moderator_ids)
    return {"removed": len(found_ids), "errors": errors}


def _batch_sync_role_after_demote(session: Session, user_ids: list[UUID]) -> None:
    """Batch-revert BOARD_ADMIN to USER for users with no remaining moderator roles."""
    admin_users = session.exec(
        select(User).where(
            User.id.in_(user_ids),
            User.role == SystemRole.BOARD_ADMIN,
        )
    ).all()
    if not admin_users:
        return
    admin_ids = [u.id for u in admin_users]
    still_mod_rows = session.exec(
        select(NamespaceModerator).where(
            NamespaceModerator.user_id.in_(admin_ids),
            NamespaceModerator.role == MemberRole.MODERATOR,
        )
    ).all()
    still_moderator_ids = {m.user_id for m in still_mod_rows}
    demote_users = [u for u in admin_users if u.id not in still_moderator_ids]
    for user in demote_users:
        user.role = SystemRole.USER
        session.add(user)
    if demote_users:
        session.commit()


# ── Role sync helpers ────────────────────────────────────────

def _sync_role_after_promote(session: Session, user: User) -> None:
    """Ensure user has BOARD_ADMIN system role when promoted to moderator."""
    if user.role == SystemRole.USER:
        user.role = SystemRole.BOARD_ADMIN
        session.add(user)


def _sync_role_after_demote(session: Session, user: User) -> None:
    """Revert to USER if no remaining moderator assignments."""
    if user.role != SystemRole.BOARD_ADMIN:
        return
    remaining = session.exec(
        select(NamespaceModerator).where(
            NamespaceModerator.user_id == user.id,
            NamespaceModerator.role == MemberRole.MODERATOR,
        )
    ).first()
    if not remaining:
        user.role = SystemRole.USER
        session.add(user)
        session.commit()


# ── Invite management ────────────────────────────────────────

def create_invite(
    session: Session,
    ns_id: UUID,
    created_by: UUID,
    role: str = "member",
    max_uses: int | None = None,
    expires_hours: int | None = 168,
) -> NamespaceInvite:
    """Generate a new invite link for a namespace."""
    code = secrets.token_urlsafe(9)
    expires_at = None
    if expires_hours is not None:
        expires_at = datetime.now(tz=_TZ8) + timedelta(hours=expires_hours)
    invite = NamespaceInvite(
        namespace_id=ns_id,
        created_by=created_by,
        code=code,
        role=role,
        max_uses=max_uses,
        expires_at=expires_at,
    )
    session.add(invite)
    session.commit()
    session.refresh(invite)
    return invite


def list_invites(session: Session, ns_id: UUID) -> list[NamespaceInvite]:
    """List active invites for a namespace."""
    stmt = select(NamespaceInvite).where(
        NamespaceInvite.namespace_id == ns_id,
        NamespaceInvite.is_active.is_(True),
    )
    return list(session.exec(stmt).all())


def revoke_invite(session: Session, invite_id: UUID) -> None:
    """Revoke an invite by setting is_active=False."""
    invite = session.get(NamespaceInvite, invite_id)
    if not invite:
        raise ValueError("邀请不存在")
    invite.is_active = False
    session.commit()


def get_invite_by_code(session: Session, code: str) -> NamespaceInvite | None:
    """Look up an invite by its code."""
    stmt = select(NamespaceInvite).where(NamespaceInvite.code == code)
    return session.exec(stmt).first()


def join_via_invite(session: Session, code: str, user: User) -> dict:
    """Join a namespace via invite code. Idempotent for existing members."""
    invite = get_invite_by_code(session, code)
    if not invite:
        raise ValueError("邀请链接无效")
    _validate_invite(invite)

    from forum_memory.models.namespace import Namespace
    ns = session.get(Namespace, invite.namespace_id)
    if not ns:
        raise ValueError("板块不存在")

    # Idempotent: if already a member, just return success
    mem = _upsert_membership(session, invite.namespace_id, user.id, invite.role)
    invite.use_count += 1
    if invite.role == MemberRole.MODERATOR:
        _sync_role_after_promote(session, user)

    # Auto-follow the board so it appears in the sidebar immediately
    _auto_follow(session, invite.namespace_id, user.id)

    session.commit()
    return {
        "namespace_id": str(ns.id),
        "namespace_display_name": ns.display_name,
        "role": mem.role,
    }


def _auto_follow(session: Session, namespace_id: UUID, user_id: UUID) -> None:
    """Ensure the user follows the board (idempotent)."""
    from forum_memory.models.board_follow import BoardFollow
    existing = session.exec(
        select(BoardFollow).where(
            BoardFollow.user_id == user_id,
            BoardFollow.namespace_id == namespace_id,
        )
    ).first()
    if not existing:
        session.add(BoardFollow(user_id=user_id, namespace_id=namespace_id))


def _validate_invite(invite: NamespaceInvite) -> None:
    """Check invite is active, not expired, and not exhausted."""
    if not invite.is_active:
        raise ValueError("邀请链接已撤销")
    if invite.expires_at:
        exp = invite.expires_at
        now = datetime.now(tz=_TZ8)
        # 兼容 naive datetime：如果数据库返回无时区信息，补上 UTC+8
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=_TZ8)
        if now > exp:
            raise ValueError("邀请链接已过期")
    if invite.max_uses is not None and invite.use_count >= invite.max_uses:
        raise ValueError("邀请链接已达到使用上限")
