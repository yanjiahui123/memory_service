"""Tests for core/quality.py — quality score computation (pure math)."""

from datetime import datetime, timezone, timedelta

from forum_memory.core.quality import compute_quality_score


def test_zero_feedback_returns_neutral_score():
    """No feedback at all should give a neutral-ish score around 0.575."""
    now = datetime.now(timezone.utc)
    score = compute_quality_score(
        useful=0, not_useful=0, wrong=0, outdated=0,
        source_role=None, retrieve_count=0, created_at=now,
    )
    # 0.30*0.5 + 0.20*0.5 + 0.15*0.5 + 0.10*0.0 + 0.15*1.0 + 0.10*1.0 = 0.575
    assert 0.55 <= score <= 0.60


def test_high_useful_ratio_boosts_score():
    now = datetime.now(timezone.utc)
    score = compute_quality_score(
        useful=20, not_useful=0, wrong=0, outdated=0,
        source_role="admin", retrieve_count=50, created_at=now,
    )
    assert score > 0.7


def test_high_wrong_count_lowers_score():
    now = datetime.now(timezone.utc)
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
    old_date = datetime.now(timezone.utc) - timedelta(days=365)
    score_old = compute_quality_score(
        useful=5, not_useful=0, wrong=0, outdated=0,
        source_role=None, retrieve_count=0, created_at=old_date,
    )
    score_new = compute_quality_score(
        useful=5, not_useful=0, wrong=0, outdated=0,
        source_role=None, retrieve_count=0, created_at=datetime.now(timezone.utc),
    )
    assert score_old < score_new


def test_citation_resolution_neutral_when_never_cited():
    """cite_count=0 should yield neutral 0.5 for citation_resolution."""
    now = datetime.now(timezone.utc)
    score = compute_quality_score(
        useful=5, not_useful=0, wrong=0, outdated=0,
        source_role=None, retrieve_count=0, created_at=now,
        cite_count=0, resolved_citation_count=0,
    )
    # Same as default — citation resolution is neutral
    assert 0.55 <= score <= 0.75


def test_high_citation_resolution_boosts_score():
    now = datetime.now(timezone.utc)
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
    now = datetime.now(timezone.utc)
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
    now = datetime.now(timezone.utc)
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
    now = datetime.now(timezone.utc)
    high_retrieval = compute_quality_score(
        useful=0, not_useful=0, wrong=0, outdated=0,
        source_role=None, retrieve_count=100, created_at=now,
    )
    zero_retrieval = compute_quality_score(
        useful=0, not_useful=0, wrong=0, outdated=0,
        source_role=None, retrieve_count=0, created_at=now,
    )
    assert high_retrieval > zero_retrieval
