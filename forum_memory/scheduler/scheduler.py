"""APScheduler setup — event polling and maintenance task scheduling.

Embeds a BackgroundScheduler into the FastAPI process. All jobs run in
background daemon threads via APScheduler's built-in ThreadPoolExecutor.
"""

import logging

from apscheduler.events import EVENT_JOB_ERROR
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from forum_memory.scheduler.event_poller import poll_and_extract
from forum_memory.scheduler.maintenance_tasks import (
    timeout_threads,
    lifecycle_memories,
    refresh_quality,
    repair_es_sync,
    reconcile_comment_counts,
    retry_failed_extractions,
)

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def init_scheduler() -> None:
    """Create, configure, and start the background scheduler."""
    global _scheduler

    _scheduler = BackgroundScheduler(
        executors={"default": ThreadPoolExecutor(max_workers=3)},
        job_defaults={"max_instances": 1, "misfire_grace_time": 300},
    )

    _scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)

    # Event-driven: extraction poller (every 30 seconds)
    _scheduler.add_job(
        poll_and_extract,
        trigger=IntervalTrigger(seconds=30),
        id="extraction_poller",
        name="Extraction event poller",
    )

    # Scheduled: thread timeout (every hour)
    _scheduler.add_job(
        timeout_threads,
        trigger=IntervalTrigger(hours=1),
        id="thread_timeout",
        name="Thread timeout check",
    )

    # Scheduled: memory lifecycle (daily at 02:00)
    _scheduler.add_job(
        lifecycle_memories,
        trigger=CronTrigger(hour=2, minute=0),
        id="memory_lifecycle",
        name="Memory lifecycle transitions",
    )

    # Scheduled: quality refresh (daily at 03:00)
    _scheduler.add_job(
        refresh_quality,
        trigger=CronTrigger(hour=3, minute=0),
        id="quality_refresh",
        name="Quality score refresh",
    )

    # Scheduled: ES sync repair (every 10 minutes)
    _scheduler.add_job(
        repair_es_sync,
        trigger=IntervalTrigger(minutes=10),
        id="es_sync_repair",
        name="ES sync repair",
    )

    # Scheduled: comment count reconciliation (daily at 04:00)
    _scheduler.add_job(
        reconcile_comment_counts,
        trigger=CronTrigger(hour=4, minute=0),
        id="comment_count_reconcile",
        name="Comment count reconciliation",
    )

    # Scheduled: retry failed/empty extractions (every hour)
    _scheduler.add_job(
        retry_failed_extractions,
        trigger=IntervalTrigger(hours=1),
        id="retry_failed_extractions",
        name="Retry failed extractions",
    )

    _scheduler.start()
    logger.info("Scheduler initialized with %d jobs", len(_scheduler.get_jobs()))


def shutdown_scheduler() -> None:
    """Gracefully stop the scheduler, waiting for running jobs to complete."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=True)
        logger.info("Scheduler shut down")
        _scheduler = None


def _on_job_error(event) -> None:
    """Log scheduler job errors."""
    logger.error(
        "[scheduler] Job %s failed: %s",
        event.job_id,
        event.exception,
        exc_info=event.exception,
    )
