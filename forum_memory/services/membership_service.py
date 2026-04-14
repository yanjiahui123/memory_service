"""Membership management service — sync."""

import logging
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
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
    try:
        with session.begin_nested():  # SAVEPOINT：回滚只影响本条，不污染外层事务
            session.flush()
    except IntegrityError:
        # 并发写入或外部目录 email 重复：savepoint 已回退，用最新信息更新已有记录
        existing = session.exec(
            select(User).where(User.employee_id == employee_id)
        ).first()
        if not existing:
            raise ValueError(f"用户 {employee_id} 数据冲突，请稍后重试")
        existing.display_name = info.get("name", employee_id)
        existing.email = info.get("email")
        existing.dept_code = info.get("dept_code")
        existing.dept_path = info.get("dept_path")
        existing.dept_levels = info.get("dept_levels")
        return existing
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
    """Batch add members by employee_id list. max_count=None means no limit.

    Strategy:
    1. One SELECT to find existing users.
    2. External lookup (one-by-one, unavoidable) for missing users only.
    3. One bulk INSERT ... ON CONFLICT DO UPDATE for users.
    4. One bulk INSERT ... ON CONFLICT DO NOTHING for memberships.
    5. One bulk INSERT ... ON CONFLICT DO NOTHING for board follows.
    6. One final commit.
    """
    ids = [eid.strip() for eid in employee_ids if eid.strip()]
    if max_count is not None:
        ids = ids[:max_count]
    if not ids:
        return {"added": 0, "skipped": 0, "errors": []}

    errors: list[str] = []

    # ── 1. Bulk-fetch existing users ─────────────────────────
    existing_map: dict[str, User] = {
        u.employee_id: u
        for u in session.exec(select(User).where(User.employee_id.in_(ids))).all()
    }

    # ── 2. External lookup for missing users ─────────────────
    now = datetime.now(tz=_TZ8)
    new_rows: list[dict] = []
    update_rows: list[dict] = []  # existing users whose info may be stale

    for eid in ids:
        info = _lookup_external(eid)
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

    # ── 3. Bulk upsert users ──────────────────────────────────
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

    # Update stale info for already-existing users
    for row in update_rows:
        user = existing_map[row["employee_id"]]
        user.display_name = row["display_name"]
        user.email = row["email"]
        user.dept_code = row["dept_code"]
        user.dept_path = row["dept_path"]
        user.dept_levels = row["dept_levels"]

    session.flush()

    # ── 4. Re-fetch all users to get their IDs ────────────────
    valid_ids = [r["employee_id"] for r in new_rows] + [r["employee_id"] for r in update_rows]
    valid_ids += [eid for eid in ids if eid in existing_map and eid not in {r["employee_id"] for r in update_rows}]
    all_users: dict[str, User] = {
        u.employee_id: u
        for u in session.exec(select(User).where(User.employee_id.in_(valid_ids))).all()
    }

    if not all_users:
        session.commit()
        return {"added": 0, "skipped": 0, "errors": errors}

    user_id_list = [u.id for u in all_users.values()]

    # ── 5. Bulk-fetch existing memberships ────────────────────
    existing_member_ids: set[UUID] = {
        m.user_id
        for m in session.exec(
            select(NamespaceModerator).where(
                NamespaceModerator.namespace_id == ns_id,
                NamespaceModerator.user_id.in_(user_id_list),
            )
        ).all()
    }

    new_mem_rows = [
        {"id": uuid4(), "user_id": u.id, "namespace_id": ns_id, "role": role,
         "created_at": now, "updated_at": now}
        for u in all_users.values()
        if u.id not in existing_member_ids
    ]
    added = len(new_mem_rows)
    skipped = len(all_users) - added

    if new_mem_rows:
        mem_stmt = pg_insert(NamespaceModerator).values(new_mem_rows)
        mem_stmt = mem_stmt.on_conflict_do_nothing(
            index_elements=["user_id", "namespace_id"],
        )
        session.exec(mem_stmt)

    # ── 6. Bulk upsert board follows ──────────────────────────
    from forum_memory.models.board_follow import BoardFollow

    follow_rows = [
        {"id": uuid4(), "user_id": u.id, "namespace_id": ns_id,
         "created_at": now, "updated_at": now}
        for u in all_users.values()
    ]
    follow_stmt = pg_insert(BoardFollow).values(follow_rows)
    follow_stmt = follow_stmt.on_conflict_do_nothing(
        index_elements=["user_id", "namespace_id"],
    )
    session.exec(follow_stmt)

    # ── 7. Role sync for new moderators ──────────────────────
    if role == MemberRole.MODERATOR:
        for u in all_users.values():
            _sync_role_after_promote(session, u)

    session.commit()
    return {"added": added, "skipped": skipped, "errors": errors}


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
    """Batch remove members from a namespace. One commit for all deletes."""
    removed, errors, ex_moderator_ids = 0, [], []
    for uid in user_ids:
        stmt = select(NamespaceModerator).where(
            NamespaceModerator.user_id == uid,
            NamespaceModerator.namespace_id == ns_id,
        )
        mem = session.exec(stmt).first()
        if not mem:
            errors.append(f"{uid}: 不是此板块成员")
            continue
        if mem.role == MemberRole.MODERATOR:
            ex_moderator_ids.append(uid)
        session.delete(mem)
        removed += 1
    session.commit()
    for uid in ex_moderator_ids:
        user = session.get(User, uid)
        if user:
            _sync_role_after_demote(session, user)
    return {"removed": removed, "errors": errors}


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
