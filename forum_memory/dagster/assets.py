"""Dagster ops/jobs for memory extraction and lifecycle automation.

The extraction pipeline is split into 7 ops for visibility in the Dagster UI:
  load_thread → compress_discussion → extract_structure → atomize_knowledge
  → quality_gate → audn_dedup → finalize_extraction

Note: AI answer generation is driven by the background ThreadPoolExecutor in
thread_service._submit_ai_answer(), not by a Dagster job.
"""

import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from dagster import op, graph, job, Config, OpExecutionContext
from sqlmodel import Session

from forum_memory.database import engine
from forum_memory.models.event import DomainEvent
from forum_memory.models.extraction import ExtractionRecord
from forum_memory.models.enums import ExtractionStatus, Authority
from forum_memory.config import get_settings

logger = logging.getLogger(__name__)


# ── Extraction pipeline (7 ops) ─────────────────────────

class ExtractConfig(Config):
    thread_id: str
    event_id: str


@op
def load_thread_op(context: OpExecutionContext, config: ExtractConfig) -> dict:
    """Load thread data, check idempotency, create extraction record."""
    from forum_memory.services.extraction_service import (
        already_extracted, cleanup_failed_record, create_record,
        build_discussion, best_answer_role,
    )
    from forum_memory.models.thread import Thread
    from forum_memory.core.state_machine import default_authority, needs_human_confirm

    thread_id = UUID(config.thread_id)
    event_id = UUID(config.event_id)

    with Session(engine) as session:
        # Idempotency check
        if already_extracted(session, thread_id):
            logger.info("Thread %s already extracted, skipping", thread_id)
            event = session.get(DomainEvent, event_id)
            if event:
                event.processed = True
                session.commit()
            context.add_output_metadata({
                "status": "skipped",
                "reason": "already_extracted",
            })
            return {"skipped": True, "thread_id": config.thread_id, "event_id": config.event_id}

        thread = session.get(Thread, thread_id)
        if not thread or not thread.resolved_type:
            event = session.get(DomainEvent, event_id)
            if event:
                event.processed = True
                session.commit()
            context.add_output_metadata({
                "status": "skipped",
                "reason": "thread_not_resolved",
            })
            return {"skipped": True, "thread_id": config.thread_id, "event_id": config.event_id}

        cleanup_failed_record(session, thread_id)
        record = create_record(session, thread)

        discussion = build_discussion(session, thread.id)
        role = best_answer_role(session, thread)
        authority = default_authority(thread.resolved_type)
        pending = needs_human_confirm(thread.resolved_type)

        context.add_output_metadata({
            "status": "loaded",
            "thread_title": thread.title[:100],
            "resolved_type": str(thread.resolved_type),
            "discussion_chars": len(discussion),
        })

        return {
            "skipped": False,
            "thread_id": config.thread_id,
            "event_id": config.event_id,
            "record_id": str(record.id),
            "record_created_at": record.created_at.isoformat(),
            "title": thread.title,
            "question": thread.content,
            "discussion": discussion,
            "namespace_id": str(thread.namespace_id),
            "environment": thread.environment,
            "best_answer_role": role,
            "authority": authority.value,
            "pending": pending,
        }


@op
def compress_discussion_op(context: OpExecutionContext, thread_data: dict) -> dict:
    """Compress long discussions (>3000 chars) to fit LLM context window."""
    if thread_data.get("skipped"):
        context.add_output_metadata({"status": "skipped"})
        return thread_data

    from forum_memory.services.extraction_service import maybe_compress
    from forum_memory.providers import get_provider

    llm = get_provider()
    discussion = thread_data["discussion"]
    compressed = maybe_compress(llm, thread_data["title"], thread_data["question"], discussion)
    was_compressed = len(compressed) < len(discussion)

    context.add_output_metadata({
        "original_chars": len(discussion),
        "compressed_chars": len(compressed),
        "was_compressed": was_compressed,
    })

    thread_data["compressed_discussion"] = compressed
    return thread_data


@op
def extract_structure_op(context: OpExecutionContext, thread_data: dict) -> dict:
    """Stage 1: Parse discussion into structured form (troubleshoot/knowledge_sharing/faq)."""
    if thread_data.get("skipped"):
        context.add_output_metadata({"status": "skipped"})
        return thread_data

    from forum_memory.services.extraction_service import stage_structure
    from forum_memory.providers import get_provider

    llm = get_provider()
    discussion = thread_data.get("compressed_discussion", thread_data["discussion"])
    structured = stage_structure(llm, thread_data["title"], thread_data["question"], discussion)

    if not structured:
        context.add_output_metadata({"status": "parse_failed", "thread_type": "unknown"})
        thread_data["no_results"] = True
        thread_data["no_results_reason"] = "Structure parsing failed"
        return thread_data

    context.add_output_metadata({
        "status": "ok",
        "thread_type": structured.get("thread_type", "unknown"),
    })

    thread_data["structured"] = structured
    return thread_data


@op
def atomize_knowledge_op(context: OpExecutionContext, thread_data: dict) -> dict:
    """Stage 2: Extract atomic knowledge points from structured analysis."""
    if thread_data.get("skipped") or thread_data.get("no_results"):
        context.add_output_metadata({"status": "skipped", "atom_count": 0})
        return thread_data

    from forum_memory.services.extraction_service import stage_atomize
    from forum_memory.providers import get_provider

    llm = get_provider()
    atoms = stage_atomize(llm, thread_data["structured"])

    if not atoms:
        context.add_output_metadata({"status": "no_atoms", "atom_count": 0})
        thread_data["no_results"] = True
        thread_data["no_results_reason"] = "Atomization produced no knowledge points"
        return thread_data

    knowledge_types = list(set(a.get("knowledge_type", "unknown") for a in atoms))
    context.add_output_metadata({
        "status": "ok",
        "atom_count": len(atoms),
        "knowledge_types": json.dumps(knowledge_types),
    })

    thread_data["atoms"] = atoms
    return thread_data


@op
def quality_gate_op(context: OpExecutionContext, thread_data: dict) -> dict:
    """Stage 3: Quality filter — remove low-value knowledge points."""
    if thread_data.get("skipped") or thread_data.get("no_results"):
        context.add_output_metadata({"status": "skipped", "facts_passed": 0})
        return thread_data

    from forum_memory.services.extraction_service import stage_gate
    from forum_memory.providers import get_provider

    llm = get_provider()
    atoms = thread_data["atoms"]
    facts = stage_gate(llm, atoms)
    gate_rate = len(facts) / len(atoms) if atoms else 0

    if not facts:
        context.add_output_metadata({
            "status": "all_filtered",
            "atoms_in": len(atoms),
            "facts_passed": 0,
            "gate_pass_rate": 0.0,
        })
        thread_data["no_results"] = True
        thread_data["no_results_reason"] = "All atoms filtered by quality gate"
        return thread_data

    context.add_output_metadata({
        "status": "ok",
        "atoms_in": len(atoms),
        "facts_passed": len(facts),
        "gate_pass_rate": round(gate_rate, 2),
    })

    thread_data["facts"] = facts
    return thread_data


@op
def audn_dedup_op(context: OpExecutionContext, thread_data: dict) -> dict:
    """AUDN per-fact: find similar memories, LLM decision, apply (ADD/UPDATE/DELETE/NONE)."""
    if thread_data.get("skipped") or thread_data.get("no_results"):
        context.add_output_metadata({"status": "skipped", "memories_created": 0})
        return {
            "thread_id": thread_data["thread_id"],
            "event_id": thread_data["event_id"],
            "record_id": thread_data.get("record_id"),
            "record_created_at": thread_data.get("record_created_at"),
            "memory_ids": [],
            "audn_stats": {"ADD": 0, "UPDATE": 0, "DELETE": 0, "NONE": 0},
            "skipped": thread_data.get("skipped", False),
            "no_results": thread_data.get("no_results", False),
        }

    from forum_memory.services.extraction_service import process_one_fact, rollback_partial_memories
    from forum_memory.models.thread import Thread
    from forum_memory.providers import get_provider

    thread_id = UUID(thread_data["thread_id"])
    authority = Authority(thread_data["authority"])
    pending = thread_data["pending"]
    facts = thread_data["facts"]
    record_created_at = datetime.fromisoformat(thread_data["record_created_at"])

    llm = get_provider()
    memory_ids: list[str] = []
    batch_created: list[dict] = []
    audn_stats = {"ADD": 0, "UPDATE": 0, "DELETE": 0, "NONE": 0}

    with Session(engine) as session:
        thread = session.get(Thread, thread_id)
        if not thread:
            raise ValueError(f"Thread {thread_id} not found in AUDN step")

        try:
            for fact in facts:
                mid, action = process_one_fact(
                    session, llm, thread, fact, authority, pending, batch_created,
                )
                audn_stats[action] = audn_stats.get(action, 0) + 1
                if mid:
                    memory_ids.append(str(mid))
                    batch_created.append({
                        "id": str(mid),
                        "content": fact["content"],
                        "authority": thread_data["authority"],
                    })
        except Exception:
            logger.exception(
                "AUDN failed for thread %s — rolling back %d partial memories",
                thread_id, len(memory_ids),
            )
            rollback_partial_memories(session, thread_id, record_created_at)
            raise

    context.add_output_metadata({
        "status": "ok",
        "facts_processed": len(facts),
        "memories_created": len(memory_ids),
        "audn_ADD": audn_stats["ADD"],
        "audn_UPDATE": audn_stats["UPDATE"],
        "audn_DELETE": audn_stats["DELETE"],
        "audn_NONE": audn_stats["NONE"],
    })

    return {
        "thread_id": thread_data["thread_id"],
        "event_id": thread_data["event_id"],
        "record_id": thread_data["record_id"],
        "record_created_at": thread_data["record_created_at"],
        "memory_ids": memory_ids,
        "audn_stats": audn_stats,
        "skipped": False,
        "no_results": False,
    }


@op
def finalize_extraction_op(context: OpExecutionContext, result: dict):
    """Mark extraction record as COMPLETED and domain event as processed."""
    event_id = UUID(result["event_id"])

    # Early skip — event already marked in load_thread_op
    if result.get("skipped") and not result.get("record_id"):
        context.add_output_metadata({"status": "skipped_early"})
        return

    with Session(engine) as session:
        # Update extraction record
        if result.get("record_id"):
            record = session.get(ExtractionRecord, UUID(result["record_id"]))
            if record:
                record.status = ExtractionStatus.COMPLETED
                memory_ids = result.get("memory_ids", [])
                record.memory_ids_created = ",".join(memory_ids)
                session.commit()

        # Mark event as processed
        event = session.get(DomainEvent, event_id)
        if event:
            event.processed = True
            session.commit()

    memory_count = len(result.get("memory_ids", []))
    context.add_output_metadata({
        "status": "completed",
        "memories_created": memory_count,
        "audn_stats": json.dumps(result.get("audn_stats", {})),
    })
    logger.info(
        "Extraction finalized: %d memories from thread %s",
        memory_count, result["thread_id"],
    )


@graph
def extract_memories_graph():
    """7-step extraction pipeline visible as separate nodes in Dagster UI."""
    loaded = load_thread_op()
    compressed = compress_discussion_op(loaded)
    structured = extract_structure_op(compressed)
    atomized = atomize_knowledge_op(structured)
    gated = quality_gate_op(atomized)
    deduped = audn_dedup_op(gated)
    finalize_extraction_op(deduped)


extract_memories_job = extract_memories_graph.to_job(name="extract_memories_job")


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
    """Transition inactive memories: ACTIVE->COLD, COLD->ARCHIVED."""
    from forum_memory.services.memory_service import transition_cold_memories, transition_archived_memories
    settings = get_settings()
    with Session(engine) as session:
        cold_count = transition_cold_memories(session, settings.cold_inactive_days)
        archive_count = transition_archived_memories(session, settings.archive_inactive_days)
        logger.info("Lifecycle: %d->COLD, %d->ARCHIVED", cold_count, archive_count)


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
