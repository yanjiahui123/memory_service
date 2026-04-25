"""Enums for the forum and memory system."""

from enum import Enum


# ── Thread enums ──────────────────────────────────────────────

class ThreadStatus(str, Enum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"              # 主动关闭（未采纳回答）
    TIMEOUT_CLOSED = "TIMEOUT_CLOSED"
    DELETED = "DELETED"


class ResolvedType(str, Enum):
    AI_RESOLVED = "ai_resolved"
    HUMAN_RESOLVED = "human_resolved"
    MANUAL_CLOSED = "manual_closed"  # 主动关闭，非解决
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


class PendingReason(str, Enum):
    """Why a memory was flagged pending_human_confirm.

    None (NULL) = legacy or unspecified. Each reason should map to a dedicated
    待处理中心 tab（互斥分类，避免多 tab 重复展示）。
    """
    # AUDN 类（新记忆需要人工审批是否入库）
    AUDN_CONFLICT = "AUDN_CONFLICT"                       # ADD + conflict_with_locked（与 LOCKED 记忆矛盾，附带 CONTRADICTS 关系）
    AUDN_SUPPLEMENT_LOCKED = "AUDN_SUPPLEMENT_LOCKED"     # UPDATE 目标为 LOCKED（新事实作为独立补充记忆入库，附带 SUPPLEMENTS 关系）
    AUDN_DELETE_LOCKED = "AUDN_DELETE_LOCKED"             # DELETE 目标为 LOCKED（原 LOCKED 记忆被 flag，新事实另行入库 + SUPERSEDES 关系）
    # 其他类
    WRONG_FEEDBACK = "WRONG_FEEDBACK"      # wrong_count 超阈值
    ADMIN_DELETE = "ADMIN_DELETE"          # 管理员删除帖子，关联记忆待复核
    TIMEOUT = "TIMEOUT"                    # 帖子超时关闭，记忆待人工评估
    LOW_QUALITY = "LOW_QUALITY"            # 质量门控未通过但置信度未低到丢弃（低质量原子，需人工评估）


# 集合：AUDN 三类（前端"AUDN 审批" tab 使用）
AUDN_PENDING_REASONS: set[str] = {
    PendingReason.AUDN_CONFLICT.value,
    PendingReason.AUDN_SUPPLEMENT_LOCKED.value,
    PendingReason.AUDN_DELETE_LOCKED.value,
}


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
    PRIVATE = "private"        # Only members can read and post


class MemberRole(str, Enum):
    """Role within a namespace membership."""
    MODERATOR = "moderator"    # Can manage board settings + members
    MEMBER = "member"          # Can read/write in private boards


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