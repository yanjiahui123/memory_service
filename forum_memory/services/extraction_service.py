"""Extraction orchestrator — sync.

Pipeline: idempotent guard → compress → extract facts → AUDN per fact → persist.
"""

import logging
from datetime import datetime
from uuid import UUID

from sqlmodel import Session, select

from forum_memory.models.thread import Thread, Comment
from forum_memory.models.extraction import ExtractionRecord
from forum_memory.models.enums import AUDNAction, ExtractionStatus, MemoryStatus
from forum_memory.core.state_machine import default_authority, needs_human_confirm
from forum_memory.core.extraction import (
    build_compress_messages,
    build_structure_messages, parse_structured_analysis,
    build_atomize_messages, parse_atomized_facts,
    build_gate_messages, parse_gated_facts,
)
from forum_memory.core.audn import AUDNResult, build_audn_messages, parse_audn_response

from forum_memory.schemas.memory import MemoryCreate
from forum_memory.services.memory_service import apply_audn
from forum_memory.services.search_service import find_similar
from forum_memory.providers import get_provider

logger = logging.getLogger(__name__)


def re_extract(session: Session, thread_id: UUID) -> list[UUID]:
    """Clear old extraction record and re-run extraction pipeline.
    Marks old memories from this thread as DELETED, then re-extracts.
    Uses SELECT FOR UPDATE to prevent concurrent re-extractions on the same thread."""
    from forum_memory.models.memory import Memory
    from forum_memory.services import es_service
    from sqlalchemy import text as sa_text

    # Lock the thread row to prevent concurrent re_extract on the same thread
    session.execute(
        sa_text("SELECT id FROM threads WHERE id = :tid FOR UPDATE NOWAIT"),
        {"tid": str(thread_id)},
    )

    # 1. Delete old extraction record
    stmt = select(ExtractionRecord).where(ExtractionRecord.thread_id == thread_id)
    old_records = list(session.exec(stmt).all())
    for rec in old_records:
        session.delete(rec)

    # 2. Soft-delete old memories sourced from this thread
    mem_stmt = select(Memory).where(Memory.source_id == thread_id, Memory.status != MemoryStatus.DELETED)
    old_memories = list(session.exec(mem_stmt).all())
    for m in old_memories:
        m.status = MemoryStatus.DELETED
        es_service.delete_memory_doc(m.id)

    session.commit()

    # 3. Re-run extraction
    return run_extraction(session, thread_id)


def run_extraction(session: Session, thread_id: UUID) -> list[UUID]:
    """Run full extraction pipeline for a resolved thread. Returns created memory IDs."""
    if already_extracted(session, thread_id):
        logger.info("Thread %s already extracted, skipping", thread_id)
        return []

    thread = session.get(Thread, thread_id)
    if not thread or not thread.resolved_type:
        raise ValueError("Thread not found or not resolved")

    # Clean up any previous FAILED record so we can retry
    cleanup_failed_record(session, thread_id)

    record = create_record(session, thread)
    try:
        memory_ids = _execute_pipeline(session, thread, record)
        record.status = ExtractionStatus.COMPLETED
        record.memory_ids_created = ",".join(str(mid) for mid in memory_ids)
        session.commit()
        return memory_ids
    except Exception as e:
        record.status = ExtractionStatus.FAILED
        record.error_message = str(e)[:500]
        # Rollback any memories created during this failed pipeline run
        rollback_partial_memories(session, thread_id, record.created_at)
        session.commit()
        raise


def already_extracted(session: Session, thread_id: UUID) -> bool:
    """Check if extraction has already completed successfully."""
    stmt = select(ExtractionRecord).where(
        ExtractionRecord.thread_id == thread_id,
        ExtractionRecord.status == ExtractionStatus.COMPLETED,
    )
    return session.exec(stmt).first() is not None


def cleanup_failed_record(session: Session, thread_id: UUID) -> None:
    """Remove FAILED and stale IN_PROGRESS extraction records to allow retry.

    IN_PROGRESS records older than 30 minutes are considered stale (process crashed).
    """
    from datetime import timedelta
    stale_cutoff = datetime.now() - timedelta(minutes=30)
    stmt = select(ExtractionRecord).where(
        ExtractionRecord.thread_id == thread_id,
        (ExtractionRecord.status == ExtractionStatus.FAILED) |
        (
            (ExtractionRecord.status == ExtractionStatus.IN_PROGRESS) &
            (ExtractionRecord.created_at < stale_cutoff)
        ),
    )
    for rec in session.exec(stmt).all():
        session.delete(rec)
    session.flush()


def rollback_partial_memories(session: Session, thread_id: UUID, since: datetime) -> None:
    """Soft-delete memories created during a failed extraction run and remove from ES."""
    from forum_memory.models.memory import Memory
    from forum_memory.services import es_service

    stmt = select(Memory).where(
        Memory.source_id == thread_id,
        Memory.status != MemoryStatus.DELETED,
        Memory.created_at >= since,
    )
    memories = list(session.exec(stmt).all())
    for m in memories:
        m.status = MemoryStatus.DELETED
        try:
            es_service.delete_memory_doc(m.id)
        except Exception:
            pass  # ES cleanup is best-effort; repair sensor will handle leftovers
    if memories:
        logger.info("Rolled back %d partial memories for thread %s", len(memories), thread_id)


def create_record(session: Session, thread: Thread) -> ExtractionRecord:
    record = ExtractionRecord(
        thread_id=thread.id,
        namespace_id=thread.namespace_id,
        status=ExtractionStatus.IN_PROGRESS,
    )
    session.add(record)
    session.commit()
    return record


def _execute_pipeline(session: Session, thread: Thread, record: ExtractionRecord) -> list[UUID]:
    """Compress → extract → AUDN → persist."""
    llm = get_provider()
    discussion = build_discussion(session, thread.id)
    compressed = maybe_compress(llm, thread.title, thread.content, discussion)
    facts = extract_facts(llm, thread.title, thread.content, compressed)

    authority = default_authority(thread.resolved_type)
    pending = needs_human_confirm(thread.resolved_type)
    memory_ids = []
    # Track memories created in this batch so later facts can see earlier ones
    # during AUDN dedup (ES may not have indexed them yet due to near-realtime delay)
    batch_created: list[dict] = []

    for fact in facts:
        mid, _action = process_one_fact(session, llm, thread, fact, authority, pending, batch_created)
        if mid:
            memory_ids.append(mid)
            batch_created.append({
                "id": str(mid),
                "content": fact["content"],
                "authority": authority.value if authority else "NORMAL",
                "tags": fact.get("tags", []),
                "knowledge_type": fact.get("knowledge_type"),
            })

    return memory_ids


def build_discussion(session: Session, thread_id: UUID) -> str:
    stmt = (
        select(Comment)
        .where(Comment.thread_id == thread_id, Comment.deleted_at.is_(None))
        .order_by(Comment.created_at)
    )
    comments = list(session.exec(stmt).all())
    parts = []
    for c in comments:
        role = "AI" if c.is_ai else c.author_role
        best = " [BEST]" if c.is_best_answer else ""
        parts.append(f"[{role}{best}]: {c.content}")
    return "\n\n".join(parts)


def maybe_compress(llm, title: str, question: str, discussion: str) -> str:
    if len(discussion) < 3000:
        return discussion
    msgs = build_compress_messages(title, question, discussion)
    return llm.complete(msgs)


def extract_facts(llm, title: str, question: str, discussion: str) -> list[dict]:
    """Three-stage extraction: Structure → Atomize → Gate."""
    structured = stage_structure(llm, title, question, discussion)
    if not structured:
        logger.warning("Stage 1 (Structure) returned no result for thread '%s'", title)
        return []

    atoms = stage_atomize(llm, structured)
    if not atoms:
        logger.warning("Stage 2 (Atomize) produced no knowledge points for thread '%s'", title)
        return []

    facts = stage_gate(llm, atoms)
    logger.info(
        "Three-stage extraction for '%s': %d atoms → %d passed gate",
        title, len(atoms), len(facts),
    )
    return facts


def stage_structure(llm, title: str, question: str, discussion: str) -> dict | None:
    """Stage 1: Parse discussion into structured intermediate form."""
    msgs = build_structure_messages(title, question, discussion)
    raw = llm.complete(msgs)
    result = parse_structured_analysis(raw)
    if not result:
        # Retry once on JSON parse failure (same pattern as AUDN retry)
        logger.info("Stage 1 parse error, retrying once — raw output: %s", raw[:300])
        raw = llm.complete(msgs)
        result = parse_structured_analysis(raw)
        if not result:
            logger.warning("Stage 1 parse error after retry — raw output: %s", raw[:300])
    return result


def stage_atomize(llm, structured: dict) -> list[dict]:
    """Stage 2: Extract atomic knowledge points from structured analysis."""
    msgs = build_atomize_messages(structured)
    raw = llm.complete(msgs)
    atoms = parse_atomized_facts(raw)
    if not atoms:
        # Retry once on JSON parse failure
        logger.info("Stage 2 parse returned empty, retrying once — raw output: %s", raw[:300])
        raw = llm.complete(msgs)
        atoms = parse_atomized_facts(raw)
    logger.debug("Stage 2 (Atomize) produced %d atoms", len(atoms))
    return atoms


def stage_gate(llm, atoms: list[dict]) -> list[dict]:
    """Stage 3: Quality gate — filter and convert to standard fact format."""
    msgs = build_gate_messages(atoms)
    raw = llm.complete(msgs)
    facts = parse_gated_facts(raw)
    if not facts:
        # Retry once on JSON parse failure
        logger.info("Stage 3 parse returned empty, retrying once — raw output: %s", raw[:300])
        raw = llm.complete(msgs)
        facts = parse_gated_facts(raw)
    logger.debug("Stage 3 (Gate): %d/%d atoms passed", len(facts), len(atoms))
    return facts


def process_one_fact(session, llm, thread, fact, authority, pending,
                      batch_created: list[dict] | None = None) -> tuple[UUID | None, str]:
    similar = find_similar(
        session, thread.namespace_id, fact["content"], top_k=15,
        tags=fact.get("tags"), knowledge_type=fact.get("knowledge_type"),
    )
    # Append memories created earlier in this batch (not yet visible in ES)
    if batch_created:
        seen_ids = {m["id"] for m in similar}
        for bc in batch_created:
            if bc["id"] not in seen_ids:
                similar.append(bc)
    msgs = build_audn_messages(fact["content"], similar)
    raw = llm.complete(msgs)
    result = parse_audn_response(raw)

    # Retry once if LLM returned unparseable output
    if "parse_error" in result.reason:
        logger.info("AUDN parse failed for thread %s, retrying once...", thread.id)
        raw = llm.complete(msgs)
        result = parse_audn_response(raw)
        # If still a parse error after retry, flag for human review
        if "parse_error" in result.reason:
            pending = True
            logger.warning("AUDN parse failed twice for thread %s — will ADD with human review", thread.id)

    # M5: Validate target_id is in the candidate list
    if result.target_id and result.action in (AUDNAction.UPDATE, AUDNAction.DELETE):
        valid_ids = {m["id"] for m in similar}
        if result.target_id not in valid_ids:
            logger.warning(
                "AUDN target_id %s not in candidate list (%d candidates), falling back to ADD",
                result.target_id, len(valid_ids),
            )
            result = AUDNResult(action=AUDNAction.ADD, reason="target_id_not_in_candidates")

    data = MemoryCreate(
        namespace_id=thread.namespace_id,
        content=fact["content"],
        knowledge_type=fact.get("knowledge_type"),
        tags=fact.get("tags"),
        environment=thread.environment,
        source_type="thread",
        source_id=thread.id,
        source_role=best_answer_role(session, thread),
        resolved_type=thread.resolved_type,
        authority=authority.value if authority else None,
        pending_human_confirm=pending,
    )

    memory = apply_audn(session, data, result)
    return (memory.id if memory else None, result.action.value)


def best_answer_role(session: Session, thread: Thread) -> str:
    if not thread.best_answer_id:
        return "unknown"
    comment = session.get(Comment, thread.best_answer_id)
    return comment.author_role if comment else "unknown"
