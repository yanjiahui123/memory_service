"""Tests for services/relation_service.py — DB integration tests."""

from uuid import uuid4

from forum_memory.models.enums import RelationType
from forum_memory.services.relation_service import (
    create_relation,
    delete_relation,
    expand_relations_for_memories,
    list_pending_relations,
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
# list_pending_relations
# ---------------------------------------------------------------------------

def test_list_pending_relations_default_covers_three_types(session, memory_factory):
    """默认覆盖 CONTRADICTS / SUPPLEMENTS / SUPERSEDES 三类，排除 CAUSED_BY 等其他类型。"""
    m1 = memory_factory()
    m2 = memory_factory()
    m3 = memory_factory()
    m4 = memory_factory()
    create_relation(session, m1.id, m2.id, RelationType.CONTRADICTS)
    create_relation(session, m1.id, m3.id, RelationType.SUPPLEMENTS)
    create_relation(session, m1.id, m4.id, RelationType.CAUSED_BY)  # 不属于 LOCKED 关联裁决
    items, total = list_pending_relations(session)
    assert total == 2
    assert {it.relation_type for it in items} == {
        RelationType.CONTRADICTS, RelationType.SUPPLEMENTS,
    }


def test_list_pending_relations_filter_by_type(session, memory_factory):
    """显式传 relation_types 时按指定类型过滤。"""
    m1 = memory_factory()
    m2 = memory_factory()
    m3 = memory_factory()
    create_relation(session, m1.id, m2.id, RelationType.CONTRADICTS)
    create_relation(session, m1.id, m3.id, RelationType.SUPPLEMENTS)
    items, total = list_pending_relations(
        session, relation_types=[RelationType.CONTRADICTS],
    )
    assert total == 1
    assert items[0].relation_type == RelationType.CONTRADICTS


def test_list_pending_relations_pagination(session, memory_factory):
    pairs = [(memory_factory(), memory_factory()) for _ in range(3)]
    for src, tgt in pairs:
        create_relation(session, src.id, tgt.id, RelationType.CONTRADICTS)
    items, total = list_pending_relations(session, page=1, size=2)
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
