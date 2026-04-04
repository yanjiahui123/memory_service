"""AUDN (Add/Update/Delete/None) cycle logic."""

import json
import logging

from forum_memory.core.prompts import AUDN_SYSTEM, AUDN_USER
from forum_memory.models.enums import AUDNAction

logger = logging.getLogger(__name__)


class AUDNResult:
    """Result of an AUDN decision."""

    def __init__(self, action: AUDNAction, target_id: str | None = None,
                 merged_content: str | None = None, reason: str = "",
                 conflict_with_locked: str | None = None):
        self.action = action
        self.target_id = target_id
        self.merged_content = merged_content
        self.reason = reason
        self.conflict_with_locked = conflict_with_locked


def build_audn_messages(new_fact: str, existing: list[dict]) -> list[dict]:
    """Build LLM messages for the AUDN decision."""
    formatted = _format_existing(existing)
    return [
        {"role": "system", "content": AUDN_SYSTEM},
        {"role": "user", "content": AUDN_USER.format(new_fact=new_fact, existing_memories=formatted)},
    ]


def parse_audn_response(raw: str) -> AUDNResult:
    """Parse LLM output into an AUDNResult."""
    text = raw.strip()
    if text.startswith("```"):
        text = _strip_fences(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.error("Failed to parse AUDN output: %s", text[:200])
        return AUDNResult(action=AUDNAction.ADD, reason="parse_error_fallback_to_add")
    return _data_to_result(data)


def _format_existing(memories: list[dict]) -> str:
    """Format existing memories for the prompt."""
    if not memories:
        return "(none)"
    lines = []
    for m in memories:
        lock = " [LOCKED]" if m.get("authority") == "LOCKED" else ""
        lines.append(f'- [{m["id"]}]{lock}: {m["content"]}')
    return "\n".join(lines)


def _strip_fences(text: str) -> str:
    lines = text.split("\n")
    if lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)


def _data_to_result(data: dict) -> AUDNResult:
    """Convert parsed dict to AUDNResult."""
    action_str = data.get("action", "ADD").upper()
    try:
        action = AUDNAction(action_str)
    except ValueError:
        logger.error("Invalid AUDN action string: %s", action_str)
        action = AUDNAction.NONE
    return AUDNResult(
        action=action,
        target_id=data.get("target_id"),
        merged_content=data.get("merged_content"),
        reason=data.get("reason", ""),
        conflict_with_locked=data.get("conflict_with_locked"),
    )
