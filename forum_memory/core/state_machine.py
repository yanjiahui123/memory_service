"""Thread state machine and authority mapping."""

from forum_memory.models.enums import (
    ThreadStatus, ResolvedType, Authority,
)

# Valid transitions: from_status -> set of allowed to_statuses
VALID_TRANSITIONS: dict[ThreadStatus, set[ThreadStatus]] = {
    ThreadStatus.OPEN: {ThreadStatus.RESOLVED, ThreadStatus.TIMEOUT_CLOSED, ThreadStatus.DELETED},
    ThreadStatus.RESOLVED: {ThreadStatus.OPEN, ThreadStatus.DELETED},
    ThreadStatus.TIMEOUT_CLOSED: {ThreadStatus.OPEN, ThreadStatus.DELETED},
    ThreadStatus.DELETED: set(),
}

# Map resolved_type to default authority for extracted memories
AUTHORITY_MAP: dict[ResolvedType, Authority] = {
    ResolvedType.HUMAN_RESOLVED: Authority.LOCKED,
    ResolvedType.AI_RESOLVED: Authority.NORMAL,
    ResolvedType.TIMEOUT: Authority.NORMAL,
}


def can_transition(current: ThreadStatus, target: ThreadStatus) -> bool:
    """Check if a transition is valid."""
    return target in VALID_TRANSITIONS.get(current, set())


def default_authority(resolved_type: ResolvedType) -> Authority:
    """Determine memory authority from resolution type."""
    return AUTHORITY_MAP.get(resolved_type, Authority.NORMAL)


def needs_human_confirm(resolved_type: ResolvedType) -> bool:
    """Whether extracted memories need human confirmation."""
    return resolved_type == ResolvedType.TIMEOUT
