"""Event-driven extraction poller.

Polls the DomainEvent table for unprocessed extraction events every 30 seconds,
routes each to the appropriate SourceAdapter, and runs the extraction pipeline.
Replaces the Dagster source_extraction_sensor + extract_memories_job.
"""

import logging
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from forum_memory.database import engine
from forum_memory.models.event import DomainEvent
from forum_memory.models.extraction import ExtractionRecord, ExtractionStatus

logger = logging.getLogger(__name__)


def poll_and_extract() -> None:
    """Poll for unprocessed extraction events and process them sequentially."""
    from forum_memory.core.source_registry import all_event_types, adapter_for_event

    target_types = all_event_types()
    if not target_types:
        return

    with Session(engine) as session:
        stmt = (
            select(DomainEvent)
            .where(
                DomainEvent.event_type.in_(target_types),
                DomainEvent.processed.is_(False),
            )
            .order_by(DomainEvent.created_at)
            .limit(20)
        )
        events = list(session.exec(stmt).all())

    if not events:
        return

    logger.info("[scheduler:extraction_poller] Found %d unprocessed events", len(events))

    processed = 0
    for event in events:
        adapter = adapter_for_event(event.event_type)
        if not adapter:
            _mark_processed(event.id)
            continue
        try:
            _extract_one(adapter.source_type(), event.aggregate_id, event.id)
            processed += 1
        except Exception:
            logger.exception(
                "[scheduler:extraction_poller] Extraction failed for %s/%s (event %s)",
                adapter.source_type(), event.aggregate_id, event.id,
            )

    if processed:
        logger.info("[scheduler:extraction_poller] Processed %d/%d events", processed, len(events))


def _extract_one(source_type: str, source_id: UUID, event_id: UUID) -> None:
    """Run extraction for one event and mark it processed."""
    from forum_memory.services.extraction_service import run_extraction

    with Session(engine) as session:
        try:
            run_extraction(session, source_type, source_id)
        except ValueError:
            # Source not ready or already extracted — expected skip
            logger.debug(
                "[scheduler:extraction_poller] Skipped %s/%s: not ready or already extracted",
                source_type, source_id,
            )
        except IntegrityError:
            # Unique constraint violation: an IN_PROGRESS record already exists.
            # Must rollback before the session can be reused.
            session.rollback()
            if _is_extraction_stale(session, source_type, source_id):
                # Stale IN_PROGRESS (>30 min) — likely a crashed run.
                # Don't mark processed; next poll will clean up and retry.
                logger.warning(
                    "[scheduler:extraction_poller] Stale IN_PROGRESS for %s/%s, will retry next poll",
                    source_type, source_id,
                )
                return
            logger.info(
                "[scheduler:extraction_poller] Extraction already in progress for %s/%s, skipping",
                source_type, source_id,
            )

        _mark_event_processed(session, event_id)


def _is_extraction_stale(
    session: Session, source_type: str, source_id: UUID,
) -> bool:
    """Check if an IN_PROGRESS extraction record is stale (>30 min)."""
    stale_cutoff = datetime.now() - timedelta(minutes=30)
    stmt = select(ExtractionRecord).where(
        ExtractionRecord.source_type == source_type,
        ExtractionRecord.source_id == source_id,
        ExtractionRecord.status == ExtractionStatus.IN_PROGRESS,
        ExtractionRecord.created_at < stale_cutoff,
    )
    return session.exec(stmt).first() is not None


def _mark_processed(event_id: UUID) -> None:
    """Mark event processed (standalone session)."""
    with Session(engine) as session:
        _mark_event_processed(session, event_id)


def _mark_event_processed(session: Session, event_id: UUID) -> None:
    """Mark a domain event as processed."""
    event = session.get(DomainEvent, event_id)
    if event:
        event.processed = True
        session.commit()
