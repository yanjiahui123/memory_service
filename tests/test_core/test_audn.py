"""Tests for core/audn.py — AUDN decision parsing (pure logic, no DB)."""

from forum_memory.core.audn import (
    AUDNResult,
    build_audn_messages,
    parse_audn_response,
    _format_existing,
)
from forum_memory.models.enums import AUDNAction


# ---------------------------------------------------------------------------
# parse_audn_response — valid JSON
# ---------------------------------------------------------------------------

def test_parse_valid_add():
    raw = '{"action": "ADD", "target_id": null, "reason": "novel fact"}'
    result = parse_audn_response(raw)
    assert result.action == AUDNAction.ADD
    assert result.target_id is None
    assert result.reason == "novel fact"


def test_parse_valid_update():
    raw = '{"action": "UPDATE", "target_id": "abc-123", "merged_content": "merged text", "reason": "extends"}'
    result = parse_audn_response(raw)
    assert result.action == AUDNAction.UPDATE
    assert result.target_id == "abc-123"
    assert result.merged_content == "merged text"


def test_parse_valid_delete():
    raw = '{"action": "DELETE", "target_id": "def-456", "reason": "obsolete"}'
    result = parse_audn_response(raw)
    assert result.action == AUDNAction.DELETE
    assert result.target_id == "def-456"


def test_parse_valid_none():
    raw = '{"action": "NONE", "reason": "already covered"}'
    result = parse_audn_response(raw)
    assert result.action == AUDNAction.NONE
    assert result.reason == "already covered"


# ---------------------------------------------------------------------------
# parse_audn_response — edge cases
# ---------------------------------------------------------------------------

def test_parse_malformed_json_falls_back_to_add():
    result = parse_audn_response("this is not valid json at all")
    assert result.action == AUDNAction.ADD
    assert "parse_error" in result.reason


def test_parse_fenced_json():
    raw = '```json\n{"action": "ADD", "reason": "fenced"}\n```'
    result = parse_audn_response(raw)
    assert result.action == AUDNAction.ADD
    assert result.reason == "fenced"


def test_parse_conflict_with_locked():
    raw = '{"action": "ADD", "conflict_with_locked": "uuid-of-locked-mem", "reason": "conflicts with locked"}'
    result = parse_audn_response(raw)
    assert result.action == AUDNAction.ADD
    assert result.conflict_with_locked == "uuid-of-locked-mem"


def test_invalid_action_string_falls_back_to_none():
    raw = '{"action": "INVALID_ACTION", "reason": "bad"}'
    result = parse_audn_response(raw)
    assert result.action == AUDNAction.NONE


def test_parse_empty_json_object():
    raw = "{}"
    result = parse_audn_response(raw)
    # action defaults to "ADD" from _data_to_result when missing
    assert result.action == AUDNAction.ADD


# ---------------------------------------------------------------------------
# _format_existing
# ---------------------------------------------------------------------------

def test_format_existing_empty():
    assert _format_existing([]) == "(none)"


def test_format_existing_with_locked_annotation():
    memories = [
        {"id": "abc-111", "content": "some locked fact", "authority": "LOCKED"},
        {"id": "def-222", "content": "normal fact"},
    ]
    text = _format_existing(memories)
    assert "[LOCKED]" in text
    assert "abc-111" in text
    assert "def-222" in text
    # Normal memory should NOT have [LOCKED]
    lines = text.split("\n")
    normal_line = [ln for ln in lines if "def-222" in ln][0]
    assert "[LOCKED]" not in normal_line


# ---------------------------------------------------------------------------
# build_audn_messages
# ---------------------------------------------------------------------------

def test_build_audn_messages_structure():
    msgs = build_audn_messages("new fact text", [])
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert "new fact text" in msgs[1]["content"]


def test_build_audn_messages_includes_existing():
    existing = [{"id": "mem-1", "content": "old fact"}]
    msgs = build_audn_messages("new fact", existing)
    assert "mem-1" in msgs[1]["content"]
    assert "old fact" in msgs[1]["content"]


# ---------------------------------------------------------------------------
# AUDNResult dataclass
# ---------------------------------------------------------------------------

def test_audn_result_defaults():
    result = AUDNResult(action=AUDNAction.ADD)
    assert result.target_id is None
    assert result.merged_content is None
    assert result.reason == ""
    assert result.conflict_with_locked is None
