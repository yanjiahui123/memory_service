"""Dagster sensors — event polling and scheduled lifecycle tasks.

Cursor management strategy:
- Event-driven sensors: cursor stores the max dispatched event ID (UUID) to avoid
  re-dispatching events after a crash. Combined with run_key deduplication.
- Scheduled sensors: cursor stores ISO timestamp of last successful dispatch
  for auditability and deduplication via run_key.
"""

import json
import logging
import uuid as _uuid
from datetime import datetime, timezone

from dagster import sensor, RunRequest, SensorEvaluationContext, SkipReason
from sqlmodel import Session, select

from forum_memory.database import engine
from forum_memory.models.event import DomainEvent

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_dispatched_ids(cursor: str | None) -> set[str]:
    """Parse dispatched event IDs from cursor JSON."""
    if not cursor:
        return set()
    try:
        cursor_data = json.loads(cursor)
        return set(cursor_data.get("dispatched", []))
    except (json.JSONDecodeError, AttributeError):
        return set()


def _prune_dispatched(session: Session, dispatched_ids: set[str]) -> set[str]:
    """Remove IDs for events that are already processed."""
    if not dispatched_ids:
        return set()
    uuid_list = [_uuid.UUID(d) for d in dispatched_ids]
    stmt = select(DomainEvent).where(
        DomainEvent.id.in_(uuid_list),
        DomainEvent.processed.is_(False),
    )
    events = session.exec(stmt).all()
    return {str(e.id) for e in events}


def _build_extract_run_request(event, source_type: str) -> RunRequest:
    """Build a RunRequest for the extraction job."""
    source_id = str(event.aggregate_id)
    event_id_str = str(event.id)
    logger.info(
        "Triggering extraction for %s/%s (event %s)", source_type, source_id, event.id,
    )
    return RunRequest(
        run_key=f"extract-{event.id}",
        run_config={
            "ops": {
                "load_source_op": {
                    "config": {
                        "source_type": source_type,
                        "source_id": source_id,
                        "event_id": event_id_str,
                    }
                }
            }
        },
    )


# ── Event-driven: extraction from any registered source adapter ─────

@sensor(job_name="extract_memories_job", minimum_interval_seconds=30)
def source_extraction_sensor(context: SensorEvaluationContext):
    """Poll for unprocessed extraction-triggering events from all registered adapters."""
    from forum_memory.core.source_registry import all_event_types, adapter_for_event

    target_event_types = all_event_types()
    if not target_event_types:
        yield SkipReason("No source adapters registered")
        return

    dispatched_ids = _load_dispatched_ids(context.cursor)

    with Session(engine) as session:
        stmt = (
            select(DomainEvent)
            .where(
                DomainEvent.event_type.in_(target_event_types),
                DomainEvent.processed.is_(False),
            )
            .order_by(DomainEvent.created_at)
            .limit(20)
        )
        events = list(session.exec(stmt).all())

        if not events:
            pruned = _prune_dispatched(session, dispatched_ids)
            if pruned != dispatched_ids:
                context.update_cursor(json.dumps({"dispatched": list(pruned)}))
            yield SkipReason("No unprocessed extraction events")
            return

        new_dispatched = False
        for event in events:
            event_id_str = str(event.id)
            if event_id_str in dispatched_ids:
                continue
            adapter = adapter_for_event(event.event_type)
            if not adapter:
                continue
            yield _build_extract_run_request(event, adapter.source_type())
            dispatched_ids.add(event_id_str)
            new_dispatched = True

        if new_dispatched:
            still_unprocessed = _prune_dispatched(session, dispatched_ids)
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
            .where(Memory.indexed_at.is_(None))
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
