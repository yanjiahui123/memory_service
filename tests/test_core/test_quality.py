"""Tests for core/quality.py — quality score computation (pure math)."""

from datetime import datetime, timezone, timedelta

from forum_memory.core.quality import compute_quality_score


def test_zero_feedback_returns_neutral_score():
    """No feedback at all should give a neutral-ish score around 0.55."""
    now = datetime.now(tz=timezone(timedelta(hours=8)))
    score = compute_quality_score(
        useful=0, not_useful=0, wrong=0, outdated=0,
        source_role=None, retrieve_count=0, created_at=now,
    )
    # 0.25*0.5 + 0.15*0.5 + 0.15*0.5 + 0.15*0.5 + 0.10*1.0 + 0.10*0.0 + 0.10*1.0 = 0.55
    assert 0.50 <= score <= 0.60


def test_high_useful_ratio_boosts_score():
    now = datetime.now(tz=timezone(timedelta(hours=8)))
    score = compute_quality_score(
        useful=20, not_useful=0, wrong=0, outdated=0,
        source_role="admin", retrieve_count=50, created_at=now,
    )
    assert score > 0.7


def test_high_wrong_count_lowers_score():
    now = datetime.now(tz=timezone(timedelta(hours=8)))
    high_wrong = compute_quality_score(
        useful=0, not_useful=0, wrong=10, outdated=0,
        source_role=None, retrieve_count=0, created_at=now,
    )
    no_wrong = compute_quality_score(
        useful=0, not_useful=0, wrong=0, outdated=0,
        source_role=None, retrieve_count=0, created_at=now,
    )
    assert high_wrong < no_wrong


def test_freshness_decay_over_365_days():
    old_date = datetime.now(tz=timezone(timedelta(hours=8))) - timedelta(days=365)
    score_old = compute_quality_score(
        useful=5, not_useful=0, wrong=0, outdated=0,
        source_role=None, retrieve_count=0, created_at=old_date,
    )
    score_new = compute_quality_score(
        useful=5, not_useful=0, wrong=0, outdated=0,
        source_role=None, retrieve_count=0, created_at=datetime.now(tz=timezone(timedelta(hours=8))),
    )
    assert score_old < score_new


def test_citation_resolution_neutral_when_never_cited():
    """cite_count=0 should yield neutral 0.5 for citation_resolution."""
    now = datetime.now(tz=timezone(timedelta(hours=8)))
    score = compute_quality_score(
        useful=5, not_useful=0, wrong=0, outdated=0,
        source_role=None, retrieve_count=0, created_at=now,
        cite_count=0, resolved_citation_count=0,
    )
    # Same as default — citation resolution is neutral
    assert 0.55 <= score <= 0.75


def test_high_citation_resolution_boosts_score():
    now = datetime.now(tz=timezone(timedelta(hours=8)))
    high_cr = compute_quality_score(
        useful=5, not_useful=0, wrong=0, outdated=0,
        source_role=None, retrieve_count=0, created_at=now,
        cite_count=10, resolved_citation_count=10,
    )
    low_cr = compute_quality_score(
        useful=5, not_useful=0, wrong=0, outdated=0,
        source_role=None, retrieve_count=0, created_at=now,
        cite_count=10, resolved_citation_count=0,
    )
    assert high_cr > low_cr


def test_admin_source_role_highest_weight():
    now = datetime.now(tz=timezone(timedelta(hours=8)))
    admin_score = compute_quality_score(
        useful=5, not_useful=0, wrong=0, outdated=0,
        source_role="admin", retrieve_count=0, created_at=now,
    )
    poster_score = compute_quality_score(
        useful=5, not_useful=0, wrong=0, outdated=0,
        source_role="poster", retrieve_count=0, created_at=now,
    )
    assert admin_score > poster_score


def test_score_always_in_01_range():
    """Score should always be clamped to [0, 1]."""
    now = datetime.now(tz=timezone(timedelta(hours=8)))
    # Best case
    best = compute_quality_score(
        useful=100, not_useful=0, wrong=0, outdated=0,
        source_role="admin", retrieve_count=200, created_at=now,
        cite_count=50, resolved_citation_count=50,
    )
    assert 0.0 <= best <= 1.0

    # Worst case
    worst = compute_quality_score(
        useful=0, not_useful=100, wrong=100, outdated=100,
        source_role=None, retrieve_count=0,
        created_at=now - timedelta(days=500),
        cite_count=50, resolved_citation_count=0,
    )
    assert 0.0 <= worst <= 1.0


def test_retrieve_heat_factor():
    now = datetime.now(tz=timezone(timedelta(hours=8)))
    high_retrieval = compute_quality_score(
        useful=0, not_useful=0, wrong=0, outdated=0,
        source_role=None, retrieve_count=100, created_at=now,
    )
    zero_retrieval = compute_quality_score(
        useful=0, not_useful=0, wrong=0, outdated=0,
        source_role=None, retrieve_count=0, created_at=now,
    )
    assert high_retrieval > zero_retrieval


def test_gate_confidence_differentiates_same_source_memories():
    """Memories from same source with different gate_confidence should get different scores.

    This is the primary reason for adding gate_confidence: at creation time,
    all other factors (feedback, retrieval, citation) are identical.
    """
    now = datetime.now(tz=timezone(timedelta(hours=8)))
    high_conf = compute_quality_score(
        useful=0, not_useful=0, wrong=0, outdated=0,
        source_role="replier", retrieve_count=0, created_at=now,
        gate_confidence=0.95,
    )
    mid_conf = compute_quality_score(
        useful=0, not_useful=0, wrong=0, outdated=0,
        source_role="replier", retrieve_count=0, created_at=now,
        gate_confidence=0.6,
    )
    low_conf = compute_quality_score(
        useful=0, not_useful=0, wrong=0, outdated=0,
        source_role="replier", retrieve_count=0, created_at=now,
        gate_confidence=0.5,
    )
    assert high_conf > mid_conf > low_conf
    # Difference should be meaningful (not just rounding)
    assert high_conf - low_conf >= 0.05


def test_gate_confidence_default_backward_compatible():
    """Default gate_confidence=0.5 should produce same result as omitting it."""
    now = datetime.now(tz=timezone(timedelta(hours=8)))
    with_default = compute_quality_score(
        useful=5, not_useful=0, wrong=0, outdated=0,
        source_role=None, retrieve_count=10, created_at=now,
        gate_confidence=0.5,
    )
    without_param = compute_quality_score(
        useful=5, not_useful=0, wrong=0, outdated=0,
        source_role=None, retrieve_count=10, created_at=now,
    )
    assert with_default == without_param
