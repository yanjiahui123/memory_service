"""Membership management service — sync."""

import logging
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

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
    """Find user by employee_id or create with info from external directory."""
    stmt = select(User).where(User.employee_id == employee_id)
    user = session.exec(stmt).first()
    if user:
        return user
    # Try external lookup for name/dept
    info = _lookup_external(employee_id)
    user = User(
        employee_id=employee_id,
        username=employee_id,
        display_name=info.get("name", employee_id) if info else employee_id,
        email=info.get("email") if info else None,
        dept_code=info.get("dept_code") if info else None,
        dept_path=info.get("dept_path") if info else None,
        dept_levels=info.get("dept_levels") if info else None,
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
    """Create membership or return existing one."""
    stmt = select(NamespaceModerator).where(
        NamespaceModerator.user_id == user_id,
        NamespaceModerator.namespace_id == ns_id,
    )
    existing = session.exec(stmt).first()
    if existing:
        return existing
    mem = NamespaceModerator(user_id=user_id, namespace_id=ns_id, role=role)
    session.add(mem)
    session.flush()
    return mem


def batch_add_members(
    session: Session,
    ns_id: UUID,
    employee_ids: list[str],
    role: str = "member",
) -> dict:
    """Batch add members by employee_id list."""
    added, skipped, errors = 0, 0, []
    for eid in employee_ids[:100]:
        eid = eid.strip()
        if not eid:
            continue
        try:
            target = _find_or_create_user(session, eid)
            mem = _upsert_membership(session, ns_id, target.id, role)
            if mem.created_at == mem.updated_at:  # newly created
                added += 1
            else:
                skipped += 1
            if role == MemberRole.MODERATOR:
                _sync_role_after_promote(session, target)
        except Exception as exc:
            errors.append(f"{eid}: {exc}")
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
    result = batch_add_members(session, ns_id, employee_ids, role)
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
    session.commit()
    return {
        "namespace_id": str(ns.id),
        "namespace_display_name": ns.display_name,
        "role": mem.role,
    }


def _validate_invite(invite: NamespaceInvite) -> None:
    """Check invite is active, not expired, and not exhausted."""
    if not invite.is_active:
        raise ValueError("邀请链接已撤销")
    if invite.expires_at and datetime.now(tz=_TZ8) > invite.expires_at:
        raise ValueError("邀请链接已过期")
    if invite.max_uses is not None and invite.use_count >= invite.max_uses:
        raise ValueError("邀请链接已达到使用上限")
