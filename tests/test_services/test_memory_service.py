"""Tests for services/memory_service.py — apply_audn and relation creation."""

import pytest

from forum_memory.core.audn import AUDNResult
from forum_memory.models.enums import (
    AUDNAction,
    Authority,
    MemoryStatus,
    RelationType,
)
from forum_memory.schemas.memory import MemoryCreate
from forum_memory.services.memory_service import apply_audn, create_memory
from forum_memory.services.relation_service import list_relations


# ---------------------------------------------------------------------------
# Auto-mock: disable ES indexing for all tests in this module
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _disable_es(monkeypatch):
    """Stub out all ES operations so tests run without Elasticsearch."""
    monkeypatch.setattr(
        "forum_memory.services.memory_service._index_to_es",
        lambda *_args, **_kwargs: True,
    )


# ---------------------------------------------------------------------------
# create_memory
# ---------------------------------------------------------------------------

def test_create_memory(session, namespace_factory):
    ns = namespace_factory()
    data = MemoryCreate(namespace_id=ns.id, content="Test knowledge")
    mem = create_memory(session, data)
    assert mem is not None
    assert mem.content == "Test knowledge"
    assert mem.status == MemoryStatus.ACTIVE


# ---------------------------------------------------------------------------
# apply_audn — ADD branch
# ---------------------------------------------------------------------------

def test_apply_audn_add(session, namespace_factory):
    ns = namespace_factory()
    new_fact = MemoryCreate(namespace_id=ns.id, content="brand new knowledge")
    result = AUDNResult(action=AUDNAction.ADD, reason="novel")
    mem = apply_audn(session, new_fact, result)
    assert mem is not None
    assert mem.content == "brand new knowledge"
    assert mem.pending_human_confirm is False


def test_apply_audn_add_conflict_creates_contradiction(session, memory_factory):
    """ADD with conflict_with_locked should flag for review and create CONTRADICTS."""
    locked = memory_factory(authority=Authority.LOCKED)
    new_fact = MemoryCreate(namespace_id=locked.namespace_id, content="conflicts with locked")
    result = AUDNResult(
        action=AUDNAction.ADD,
        conflict_with_locked=str(locked.id),
        reason="conflicts",
    )
    mem = apply_audn(session, new_fact, result)
    assert mem is not None
    assert mem.pending_human_confirm is True

    # Verify CONTRADICTS relation was created
    rels = list_relations(session, mem.id)
    contradicts = [r for r in rels if r.relation_type == RelationType.CONTRADICTS]
    assert len(contradicts) == 1
    assert contradicts[0].target_memory_id == locked.id


# ---------------------------------------------------------------------------
# apply_audn — UPDATE branch
# ---------------------------------------------------------------------------

def test_apply_audn_update_on_locked_creates_supplements(session, memory_factory):
    """UPDATE on a LOCKED target should create independent memory + SUPPLEMENTS."""
    locked = memory_factory(authority=Authority.LOCKED, content="original locked")
    new_fact = MemoryCreate(namespace_id=locked.namespace_id, content="extended view")
    result = AUDNResult(
        action=AUDNAction.UPDATE,
        target_id=str(locked.id),
        merged_content="merged content",
        reason="extends locked",
    )
    mem = apply_audn(session, new_fact, result)
    assert mem is not None
    # Should be a NEW memory, not the locked one
    assert mem.id != locked.id

    # Verify SUPPLEMENTS relation
    rels = list_relations(session, mem.id)
    supplements = [r for r in rels if r.relation_type == RelationType.SUPPLEMENTS]
    assert len(supplements) == 1


# ---------------------------------------------------------------------------
# apply_audn — DELETE branch
# ---------------------------------------------------------------------------

def test_apply_audn_delete_creates_supersedes(session, memory_factory):
    """DELETE should soft-delete old, create new, and link with SUPERSEDES."""
    old = memory_factory(content="obsolete knowledge")
    new_fact = MemoryCreate(namespace_id=old.namespace_id, content="replacement")
    result = AUDNResult(
        action=AUDNAction.DELETE,
        target_id=str(old.id),
        reason="obsolete",
    )
    mem = apply_audn(session, new_fact, result)
    assert mem is not None

    # Old memory should be soft-deleted
    session.refresh(old)
    assert old.status == MemoryStatus.DELETED

    # Verify SUPERSEDES relation
    rels = list_relations(session, mem.id)
    supersedes = [r for r in rels if r.relation_type == RelationType.SUPERSEDES]
    assert len(supersedes) == 1
    assert supersedes[0].target_memory_id == old.id


# ---------------------------------------------------------------------------
# apply_audn — NONE branch
# ---------------------------------------------------------------------------

def test_apply_audn_none_returns_none(session, namespace_factory):
    ns = namespace_factory()
    new_fact = MemoryCreate(namespace_id=ns.id, content="duplicate")
    result = AUDNResult(action=AUDNAction.NONE, reason="already covered")
    assert apply_audn(session, new_fact, result) is None
