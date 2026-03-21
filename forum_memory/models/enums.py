"""Enums for the forum and memory system."""

from enum import Enum


# ── Thread enums ──────────────────────────────────────────────

class ThreadStatus(str, Enum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    TIMEOUT_CLOSED = "TIMEOUT_CLOSED"
    DELETED = "DELETED"


class ResolvedType(str, Enum):
    AI_RESOLVED = "ai_resolved"
    HUMAN_RESOLVED = "human_resolved"
    TIMEOUT = "timeout"


# ── Memory enums ──────────────────────────────────────────────

class Authority(str, Enum):
    LOCKED = "LOCKED"
    NORMAL = "NORMAL"


class MemoryStatus(str, Enum):
    ACTIVE = "ACTIVE"
    COLD = "COLD"
    ARCHIVED = "ARCHIVED"
    DELETED = "DELETED"


class AUDNAction(str, Enum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    NONE = "NONE"


# ── User / Role enums ────────────────────────────────────────

class UserRole(str, Enum):
    """Memory source role — who provided the answer."""
    POSTER = "poster"
    COMMENTER = "commenter"
    AI = "ai"
    ADMIN = "admin"


ROLE_WEIGHT: dict[UserRole, float] = {
    UserRole.ADMIN: 1.0,
    UserRole.COMMENTER: 0.7,
    UserRole.AI: 0.5,
    UserRole.POSTER: 0.7,
}


class SystemRole(str, Enum):
    """System-level user role for access control."""
    SUPER_ADMIN = "super_admin"
    BOARD_ADMIN = "board_admin"
    USER = "user"


class AccessMode(str, Enum):
    """Namespace access mode."""
    PUBLIC = "public"          # Anyone can read and post
    RESTRICTED = "restricted"  # Anyone can read, only members can post
    PRIVATE = "private"        # Only members can read and post


# ── Feedback enums ────────────────────────────────────────────

class FeedbackType(str, Enum):
    USEFUL = "useful"
    NOT_USEFUL = "not_useful"
    WRONG = "wrong"
    OUTDATED = "outdated"


# ── Knowledge type ────────────────────────────────────────────

class KnowledgeType(str, Enum):
    HOW_TO = "how_to"
    TROUBLESHOOT = "troubleshoot"
    BEST_PRACTICE = "best_practice"
    GOTCHA = "gotcha"
    FAQ = "faq"


# ── Relation type ────────────────────────────────────────────

class RelationType(str, Enum):
    SUPPLEMENTS = "SUPPLEMENTS"   # A 补充 B
    CONTRADICTS = "CONTRADICTS"   # A 与 B 矛盾
    SUPERSEDES = "SUPERSEDES"     # A 取代 B
    CAUSED_BY = "CAUSED_BY"       # A 的原因是 B


# ── Extraction status ────────────────────────────────────────

class ExtractionStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    COMPLETED_EMPTY = "COMPLETED_EMPTY"
    FAILED = "FAILED"


# ── Operation type ────────────────────────────────────────────

class OperationType(str, Enum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    MERGE = "MERGE"
    PROMOTE = "PROMOTE"
    DEMOTE = "DEMOTE"
    ARCHIVE = "ARCHIVE"
    RESTORE = "RESTORE"


# ── Priority ──────────────────────────────────────────────────

class Priority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"