"""Dagster sensors — event polling and scheduled lifecycle tasks.

Cursor management strategy:
- Event-driven sensors: cursor stores the max dispatched event ID (UUID) to avoid
  re-dispatching events after a crash. Combined with run_key deduplication.
- Scheduled sensors: cursor stores ISO timestamp of last successful dispatch
  for auditability and deduplication via run_key.
"""

import json
import logging
from datetime import datetime, timezone

from dagster import sensor, RunRequest, SensorEvaluationContext, SkipReason
from sqlmodel import Session, select

from forum_memory.database import engine
from forum_memory.models.event import DomainEvent

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Event-driven: thread.resolved / thread.timeout_closed → extract memories ─────

@sensor(job_name="extract_memories_job", minimum_interval_seconds=30)
def thread_resolved_sensor(context: SensorEvaluationContext):
    """Poll for unprocessed thread.resolved and thread.timeout_closed events and trigger extraction.

    Cursor tracks the set of already-dispatched event IDs to prevent re-dispatching
    events whose jobs haven't marked them as processed yet (e.g. after a crash).
    """
    # Load dispatched event IDs from cursor
    dispatched_ids: set[str] = set()
    if context.cursor:
        try:
            cursor_data = json.loads(context.cursor)
            dispatched_ids = set(cursor_data.get("dispatched", []))
        except (json.JSONDecodeError, AttributeError):
            dispatched_ids = set()

    with Session(engine) as session:
        stmt = (
            select(DomainEvent)
            .where(
                DomainEvent.event_type.in_(["thread.resolved", "thread.timeout_closed"]),
                DomainEvent.processed == False,  # noqa: E712
            )
            .order_by(DomainEvent.created_at)
            .limit(20)
        )
        events = list(session.exec(stmt).all())

        if not events:
            # Prune dispatched set: remove IDs for events that are now processed
            if dispatched_ids:
                still_unprocessed = {str(e.id) for e in session.exec(
                    select(DomainEvent).where(
                        DomainEvent.id.in_([__import__('uuid').UUID(d) for d in dispatched_ids]),
                        DomainEvent.processed == False,  # noqa: E712
                    )
                ).all()}
                if still_unprocessed != dispatched_ids:
                    context.update_cursor(json.dumps({"dispatched": list(still_unprocessed)}))
            yield SkipReason("No unprocessed thread.resolved / thread.timeout_closed events")
            return

        new_dispatched = False
        for event in events:
            event_id_str = str(event.id)
            if event_id_str in dispatched_ids:
                continue  # Already dispatched, skip

            thread_id = str(event.aggregate_id)
            logger.info("Triggering extraction for thread %s (event %s)", thread_id, event.id)
            yield RunRequest(
                run_key=f"extract-{event.id}",
                run_config={
                    "ops": {
                        "load_thread_op": {
                            "config": {
                                "thread_id": thread_id,
                                "event_id": event_id_str,
                            }
                        }
                    }
                },
            )
            dispatched_ids.add(event_id_str)
            new_dispatched = True

        if new_dispatched:
            # Prune dispatched set: remove IDs for events already processed
            still_unprocessed = {str(e.id) for e in session.exec(
                select(DomainEvent).where(
                    DomainEvent.id.in_([__import__('uuid').UUID(d) for d in dispatched_ids]),
                    DomainEvent.processed == False,  # noqa: E712
                )
            ).all()}
            context.update_cursor(json.dumps({"dispatched": list(still_unprocessed)}))


# ── Scheduled: thread timeout (every hour) ───────────────

@sensor(job_name="timeout_threads_job", minimum_interval_seconds=3600)
def thread_timeout_sensor(context: SensorEvaluationContext):
    """Periodically trigger thread timeout-close check."""
    ts = _now_iso()
    yield RunRequest(run_key=f"timeout-{ts}")
    context.update_cursor(ts)


# ── Scheduled: memory lifecycle (daily) ──────────────────

@sensor(job_name="lifecycle_memories_job", minimum_interval_seconds=86400)
def memory_lifecycle_sensor(context: SensorEvaluationContext):
    """Daily trigger for memory COLD/ARCHIVED transitions."""
    ts = _now_iso()
    yield RunRequest(run_key=f"lifecycle-{ts}")
    context.update_cursor(ts)


# ── Scheduled: quality refresh (daily) ───────────────────

@sensor(job_name="refresh_quality_job", minimum_interval_seconds=86400)
def quality_refresh_sensor(context: SensorEvaluationContext):
    """Daily trigger for quality score refresh."""
    ts = _now_iso()
    yield RunRequest(run_key=f"quality-{ts}")
    context.update_cursor(ts)


# ── Scheduled: ES sync repair (every 10 minutes) ────────

@sensor(job_name="repair_es_sync_job", minimum_interval_seconds=600)
def es_sync_repair_sensor(context: SensorEvaluationContext):
    """Periodically repair DB-ES consistency gaps (re-index memories with indexed_at IS NULL)."""
    from forum_memory.models.memory import Memory
    from forum_memory.models.enums import MemoryStatus

    with Session(engine) as session:
        from sqlmodel import func
        count = session.exec(
            select(func.count())
            .select_from(Memory)
            .where(Memory.status == MemoryStatus.ACTIVE)
            .where(Memory.indexed_at == None)  # noqa: E711
        ).one()

    if count == 0:
        yield SkipReason("No unsynced memories found")
        return

    ts = _now_iso()
    logger.info("Found %d unsynced memories, triggering ES repair", count)
    yield RunRequest(run_key=f"es-repair-{ts}")
    context.update_cursor(ts)


# ── Scheduled: comment_count reconciliation (daily) ─────

@sensor(job_name="reconcile_comment_counts_job", minimum_interval_seconds=86400)
def comment_count_reconcile_sensor(context: SensorEvaluationContext):
    """Daily trigger to fix drifted comment_count values."""
    ts = _now_iso()
    yield RunRequest(run_key=f"comment-count-{ts}")
    context.update_cursor(ts)
