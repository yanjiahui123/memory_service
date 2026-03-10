"""Dagster Definitions entry point.

Start with:
    dagster dev -m forum_memory.dagster.definitions

AI answer generation is NOT managed by Dagster — it runs in a background
ThreadPoolExecutor (see thread_service._submit_ai_answer). The frontend
receives a push notification via SSE (/threads/{id}/ai-answer/stream).
"""

from dagster import Definitions

from forum_memory.dagster.assets import (
    extract_memories_job,
    timeout_threads_job,
    lifecycle_memories_job,
    refresh_quality_job,
    repair_es_sync_job,
    reconcile_comment_counts_job,
)
from forum_memory.dagster.sensors import (
    thread_resolved_sensor,
    thread_timeout_sensor,
    memory_lifecycle_sensor,
    quality_refresh_sensor,
    es_sync_repair_sensor,
    comment_count_reconcile_sensor,
)

defs = Definitions(
    jobs=[
        extract_memories_job,
        timeout_threads_job,
        lifecycle_memories_job,
        refresh_quality_job,
        repair_es_sync_job,
        reconcile_comment_counts_job,
    ],
    sensors=[
        thread_resolved_sensor,
        thread_timeout_sensor,
        memory_lifecycle_sensor,
        quality_refresh_sensor,
        es_sync_repair_sensor,
        comment_count_reconcile_sensor,
    ],
)
