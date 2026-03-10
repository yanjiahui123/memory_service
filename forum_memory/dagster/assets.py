"""Dagster ops/jobs for memory extraction and lifecycle automation.

Note: AI answer generation is driven by the background ThreadPoolExecutor in
thread_service._submit_ai_answer(), not by a Dagster job. The SSE endpoint
/threads/{id}/ai-answer/stream allows the frontend to receive a push notification
when the answer is ready without polling.
"""

import logging
from uuid import UUID

from dagster import op, job, Config
from sqlmodel import Session

from forum_memory.database import engine
from forum_memory.models.event import DomainEvent
from forum_memory.config import get_settings

logger = logging.getLogger(__name__)


# ── Extraction job (delegates to extraction_service) ──────

class ExtractConfig(Config):
    thread_id: str
    event_id: str


@op
def run_extraction_op(config: ExtractConfig):
    """Run the full extraction pipeline via extraction_service (single source of truth)."""
    from forum_memory.services.extraction_service import run_extraction

    thread_id = UUID(config.thread_id)
    event_id = UUID(config.event_id)

    with Session(engine) as session:
        succeeded = False
        try:
            memory_ids = run_extraction(session, thread_id)
            logger.info(
                "Extraction completed: %d memories from thread %s",
                len(memory_ids), thread_id,
            )
            succeeded = True
        except ValueError as e:
            # ValueError = expected skip (already extracted, not resolved, etc.)
            logger.warning("Extraction skipped for thread %s: %s", thread_id, e)
            succeeded = True  # Not a transient failure, mark as processed
        except Exception:
            logger.exception("Extraction failed for thread %s — event will be retried", thread_id)

        # Only mark event processed on success or expected skip;
        # transient failures leave the event unprocessed for retry
        if succeeded:
            event = session.get(DomainEvent, event_id)
            if event:
                event.processed = True
                session.commit()


@job
def extract_memories_job():
    run_extraction_op()


# ── Thread Timeout ───────────────────────────────────────

@op
def timeout_threads_op():
    """Batch timeout-close OPEN threads past the configured timeout."""
    from forum_memory.services.thread_service import batch_timeout_threads
    settings = get_settings()
    with Session(engine) as session:
        count = batch_timeout_threads(session, settings.thread_timeout_days)
        logger.info("Timeout-closed %d threads", count)


@job
def timeout_threads_job():
    timeout_threads_op()


# ── Memory Lifecycle ─────────────────────────────────────

@op
def lifecycle_memories_op():
    """Transition inactive memories: ACTIVE→COLD, COLD→ARCHIVED."""
    from forum_memory.services.memory_service import transition_cold_memories, transition_archived_memories
    settings = get_settings()
    with Session(engine) as session:
        cold_count = transition_cold_memories(session, settings.cold_inactive_days)
        archive_count = transition_archived_memories(session, settings.archive_inactive_days)
        logger.info("Lifecycle: %d→COLD, %d→ARCHIVED", cold_count, archive_count)


@job
def lifecycle_memories_job():
    lifecycle_memories_op()


# ── Quality Refresh ──────────────────────────────────────

@op
def refresh_quality_op():
    """Refresh quality scores for all ACTIVE memories."""
    from forum_memory.services.memory_service import bulk_refresh_quality
    with Session(engine) as session:
        count = bulk_refresh_quality(session)
        logger.info("Refreshed quality for %d memories", count)


@job
def refresh_quality_job():
    refresh_quality_op()


# ── ES Sync Repair ──────────────────────────────────────

@op
def repair_es_sync_op():
    """Repair DB-ES consistency: re-index ACTIVE memories with indexed_at IS NULL."""
    from forum_memory.services.memory_service import reindex_unsynced_memories
    with Session(engine) as session:
        count = reindex_unsynced_memories(session, batch_size=100)
        logger.info("ES sync repair: re-indexed %d memories", count)


@job
def repair_es_sync_job():
    repair_es_sync_op()


# ── Comment Count Reconciliation ────────────────────────

@op
def reconcile_comment_counts_op():
    """Fix drifted comment_count values against actual Comment rows."""
    from forum_memory.services.thread_service import reconcile_comment_counts
    with Session(engine) as session:
        count = reconcile_comment_counts(session)
        logger.info("Reconciled comment_count for %d threads", count)


@job
def reconcile_comment_counts_job():
    reconcile_comment_counts_op()
