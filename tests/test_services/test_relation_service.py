"""Tests for services/relation_service.py — DB integration tests."""

from uuid import uuid4

from forum_memory.models.enums import RelationType
from forum_memory.services.relation_service import (
    create_relation,
    delete_relation,
    expand_relations_for_memories,
    list_contradictions,
    list_relations,
)


# ---------------------------------------------------------------------------
# create_relation
# ---------------------------------------------------------------------------

def test_create_relation_success(session, memory_factory):
    m1 = memory_factory()
    m2 = memory_factory()
    rel = create_relation(session, m1.id, m2.id, RelationType.SUPPLEMENTS)
    assert rel is not None
    assert rel.source_memory_id == m1.id
    assert rel.target_memory_id == m2.id
    assert rel.relation_type == RelationType.SUPPLEMENTS
    assert rel.confidence == 1.0
    assert rel.origin == "audn"


def test_create_relation_self_returns_none(session, memory_factory):
    mem = memory_factory()
    result = create_relation(session, mem.id, mem.id, RelationType.SUPPLEMENTS)
    assert result is None


def test_create_relation_idempotent(session, memory_factory):
    m1 = memory_factory()
    m2 = memory_factory()
    r1 = create_relation(session, m1.id, m2.id, RelationType.CONTRADICTS)
    r2 = create_relation(session, m1.id, m2.id, RelationType.CONTRADICTS)
    assert r1.id == r2.id


def test_create_relation_nonexistent_memory_returns_none(session, memory_factory):
    mem = memory_factory()
    fake_id = uuid4()
    result = create_relation(session, mem.id, fake_id, RelationType.SUPPLEMENTS)
    assert result is None


def test_create_relation_custom_confidence_and_origin(session, memory_factory):
    m1 = memory_factory()
    m2 = memory_factory()
    rel = create_relation(
        session, m1.id, m2.id, RelationType.CAUSED_BY,
        confidence=0.8, origin="manual",
    )
    assert rel.confidence == 0.8
    assert rel.origin == "manual"


# ---------------------------------------------------------------------------
# list_relations
# ---------------------------------------------------------------------------

def test_list_relations_bidirectional(session, memory_factory):
    m1 = memory_factory()
    m2 = memory_factory()
    create_relation(session, m1.id, m2.id, RelationType.SUPPLEMENTS)
    # m2 should see the relation when listed
    rels_m2 = list_relations(session, m2.id)
    assert len(rels_m2) == 1
    # m1 should also see it
    rels_m1 = list_relations(session, m1.id)
    assert len(rels_m1) == 1


# ---------------------------------------------------------------------------
# expand_relations_for_memories
# ---------------------------------------------------------------------------

def test_expand_relations_batch(session, memory_factory):
    m1 = memory_factory()
    m2 = memory_factory()
    m3 = memory_factory()
    create_relation(session, m1.id, m2.id, RelationType.SUPPLEMENTS)
    create_relation(session, m1.id, m3.id, RelationType.SUPERSEDES)
    result = expand_relations_for_memories(session, [m1.id])
    assert m1.id in result
    assert len(result[m1.id]) == 2


def test_expand_relations_empty_input(session):
    result = expand_relations_for_memories(session, [])
    assert result == {}


def test_expand_relations_no_relations(session, memory_factory):
    mem = memory_factory()
    result = expand_relations_for_memories(session, [mem.id])
    assert result == {}


# ---------------------------------------------------------------------------
# list_contradictions
# ---------------------------------------------------------------------------

def test_list_contradictions_only_contradicts(session, memory_factory):
    m1 = memory_factory()
    m2 = memory_factory()
    m3 = memory_factory()
    create_relation(session, m1.id, m2.id, RelationType.CONTRADICTS)
    create_relation(session, m1.id, m3.id, RelationType.SUPPLEMENTS)
    items, total = list_contradictions(session)
    assert total == 1
    assert items[0].relation_type == RelationType.CONTRADICTS


def test_list_contradictions_pagination(session, memory_factory):
    pairs = [(memory_factory(), memory_factory()) for _ in range(3)]
    for src, tgt in pairs:
        create_relation(session, src.id, tgt.id, RelationType.CONTRADICTS)
    items, total = list_contradictions(session, page=1, size=2)
    assert total == 3
    assert len(items) == 2


# ---------------------------------------------------------------------------
# delete_relation
# ---------------------------------------------------------------------------

def test_delete_relation_success(session, memory_factory):
    m1 = memory_factory()
    m2 = memory_factory()
    rel = create_relation(session, m1.id, m2.id, RelationType.SUPPLEMENTS)
    assert delete_relation(session, rel.id) is True


def test_delete_relation_nonexistent(session):
    assert delete_relation(session, uuid4()) is False
