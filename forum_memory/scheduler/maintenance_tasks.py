"""Scheduled maintenance tasks.

Replaces the 5 Dagster scheduled sensors + single-op jobs.
Each function manages its own DB session and logs results.
"""

import logging

from sqlmodel import Session

from forum_memory.config import get_settings
from forum_memory.database import engine

logger = logging.getLogger(__name__)


def timeout_threads() -> None:
    """Batch timeout-close OPEN threads past the configured TTL."""
    from forum_memory.services.thread_service import batch_timeout_threads

    settings = get_settings()
    with Session(engine) as session:
        count = batch_timeout_threads(session, settings.thread_timeout_days)
    if count:
        logger.info("[scheduler:thread_timeout] Closed %d threads", count)


def lifecycle_memories() -> None:
    """Transition inactive memories: ACTIVE -> COLD, COLD -> ARCHIVED."""
    from forum_memory.services.memory_service import (
        transition_cold_memories,
        transition_archived_memories,
    )

    settings = get_settings()
    with Session(engine) as session:
        cold = transition_cold_memories(session, settings.cold_inactive_days)
        archived = transition_archived_memories(session, settings.archive_inactive_days)
    if cold or archived:
        logger.info("[scheduler:lifecycle] %d->COLD, %d->ARCHIVED", cold, archived)


def refresh_quality() -> None:
    """Refresh quality scores for all ACTIVE memories."""
    from forum_memory.services.memory_service import bulk_refresh_quality

    with Session(engine) as session:
        count = bulk_refresh_quality(session)
    if count:
        logger.info("[scheduler:quality] Refreshed %d memories", count)


def repair_es_sync() -> None:
    """Re-index ACTIVE memories with indexed_at IS NULL."""
    from forum_memory.services.memory_service import reindex_unsynced_memories

    with Session(engine) as session:
        count = reindex_unsynced_memories(session, batch_size=100)
    if count:
        logger.info("[scheduler:es_repair] Re-indexed %d memories", count)


def reconcile_comment_counts() -> None:
    """Fix drifted comment_count values against actual Comment rows."""
    from forum_memory.services.thread_service import reconcile_comment_counts as _reconcile

    with Session(engine) as session:
        count = _reconcile(session)
    if count:
        logger.info("[scheduler:comment_count] Reconciled %d threads", count)


def retry_failed_extractions() -> None:
    """Retry FAILED and COMPLETED_EMPTY extractions that haven't exhausted retries."""
    from sqlmodel import select
    from forum_memory.models.extraction import ExtractionRecord
    from forum_memory.models.enums import ExtractionStatus
    from forum_memory.services.extraction_service import (
        run_extraction, MAX_RETRY_COUNT,
    )

    retryable_statuses = [ExtractionStatus.FAILED, ExtractionStatus.COMPLETED_EMPTY]
    with Session(engine) as session:
        stmt = (
            select(ExtractionRecord)
            .where(
                ExtractionRecord.status.in_(retryable_statuses),
                ExtractionRecord.retry_count < MAX_RETRY_COUNT,
            )
            .order_by(ExtractionRecord.created_at)
            .limit(10)
        )
        records = list(session.exec(stmt).all())

    if not records:
        return

    logger.info("[scheduler:retry_extraction] Found %d retryable records", len(records))
    retried = 0
    for rec in records:
        try:
            with Session(engine) as session:
                run_extraction(session, rec.source_type, rec.source_id)
                retried += 1
        except Exception:
            logger.exception(
                "[scheduler:retry_extraction] Retry failed for %s/%s (attempt %d)",
                rec.source_type, rec.source_id, rec.retry_count + 1,
            )

    if retried:
        logger.info("[scheduler:retry_extraction] Successfully retried %d/%d", retried, len(records))
