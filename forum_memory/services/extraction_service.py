"""Extraction orchestrator — sync.

Pipeline: idempotent guard → compress → extract facts → AUDN per fact → persist.

Source-agnostic: all source-specific logic lives in SourceAdapter implementations.
The pipeline operates on SourceContext (title, question, discussion).
"""

import logging
from datetime import datetime, timedelta
from uuid import UUID

from sqlmodel import Session, select

from forum_memory.core.source_context import SourceContext
from forum_memory.core.source_registry import get_adapter
from forum_memory.models.extraction import ExtractionRecord
from forum_memory.models.enums import AUDNAction, ExtractionStatus, MemoryStatus
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

MAX_RETRY_COUNT = 3


def re_extract(session: Session, source_type: str, source_id: UUID) -> list[UUID]:
    """Clear old extraction record and re-run extraction pipeline.

    Marks old memories from this source as DELETED, then re-extracts.
    Uses adapter.lock_for_re_extract() to prevent concurrent re-extractions.
    """
    from forum_memory.models.memory import Memory
    from forum_memory.services import es_service

    adapter = get_adapter(source_type)
    adapter.lock_for_re_extract(session, source_id)

    _delete_old_records(session, source_type, source_id)
    _soft_delete_old_memories(session, source_id, es_service)
    session.commit()

    return run_extraction(session, source_type, source_id)


def _delete_old_records(session: Session, source_type: str, source_id: UUID) -> None:
    """Delete old extraction records for the given source."""
    stmt = select(ExtractionRecord).where(
        ExtractionRecord.source_type == source_type,
        ExtractionRecord.source_id == source_id,
    )
    for rec in session.exec(stmt).all():
        session.delete(rec)


def _soft_delete_old_memories(session, source_id: UUID, es_service) -> None:
    """Soft-delete old memories sourced from this source and remove from ES."""
    from forum_memory.models.memory import Memory

    mem_stmt = select(Memory).where(
        Memory.source_id == source_id,
        Memory.status != MemoryStatus.DELETED,
    )
    for m in session.exec(mem_stmt).all():
        m.status = MemoryStatus.DELETED
        es_service.delete_memory_doc(m.id)


def run_extraction(session: Session, source_type: str, source_id: UUID) -> list[UUID]:
    """Run full extraction pipeline for a resolved source. Returns created memory IDs."""
    if already_extracted(session, source_type, source_id):
        logger.info("Source %s/%s already extracted, skipping", source_type, source_id)
        return []

    adapter = get_adapter(source_type)
    ctx = adapter.load_context(session, source_id)
    if ctx is None:
        raise ValueError(f"Source {source_type}/{source_id} not found or not ready")

    prev_retry = _cleanup_retryable_record(session, source_type, source_id)

    record = _create_record(session, ctx, prev_retry)
    try:
        memory_ids = _execute_pipeline(session, ctx, record)
        if memory_ids:
            record.status = ExtractionStatus.COMPLETED
        else:
            record.status = ExtractionStatus.COMPLETED_EMPTY
            logger.warning(
                "Extraction produced 0 memories for %s/%s (retry %d/%d)",
                source_type, source_id, record.retry_count, MAX_RETRY_COUNT,
            )
        record.memory_ids_created = ",".join(str(mid) for mid in memory_ids)
        session.commit()
        return memory_ids
    except Exception as exc:
        record.status = ExtractionStatus.FAILED
        record.error_message = str(exc)[:500]
        rollback_partial_memories(session, source_id, record.created_at)
        session.commit()
        raise


def already_extracted(session: Session, source_type: str, source_id: UUID) -> bool:
    """Check if extraction has already completed successfully (with memories)."""
    stmt = select(ExtractionRecord).where(
        ExtractionRecord.source_type == source_type,
        ExtractionRecord.source_id == source_id,
        ExtractionRecord.status == ExtractionStatus.COMPLETED,
    )
    return session.exec(stmt).first() is not None


def has_reached_retry_limit(session: Session, source_type: str, source_id: UUID) -> bool:
    """Check if extraction has exhausted all retries (FAILED or COMPLETED_EMPTY)."""
    stmt = select(ExtractionRecord).where(
        ExtractionRecord.source_type == source_type,
        ExtractionRecord.source_id == source_id,
        ExtractionRecord.status.in_([ExtractionStatus.FAILED, ExtractionStatus.COMPLETED_EMPTY]),
        ExtractionRecord.retry_count >= MAX_RETRY_COUNT,
    )
    return session.exec(stmt).first() is not None


def _cleanup_retryable_record(session: Session, source_type: str, source_id: UUID) -> int:
    """Remove retryable extraction records and return the previous retry_count.

    Retryable: FAILED, COMPLETED_EMPTY (under retry limit), stale IN_PROGRESS (>30 min).
    """
    stale_cutoff = datetime.now() - timedelta(minutes=30)
    retryable = (
        (ExtractionRecord.status == ExtractionStatus.FAILED) |
        (ExtractionRecord.status == ExtractionStatus.COMPLETED_EMPTY) |
        (
            (ExtractionRecord.status == ExtractionStatus.IN_PROGRESS) &
            (ExtractionRecord.created_at < stale_cutoff)
        )
    )
    stmt = select(ExtractionRecord).where(
        ExtractionRecord.source_type == source_type,
        ExtractionRecord.source_id == source_id,
        retryable,
    )
    prev_retry = 0
    for rec in session.exec(stmt).all():
        prev_retry = max(prev_retry, rec.retry_count)
        session.delete(rec)
    session.flush()
    return prev_retry


def rollback_partial_memories(session: Session, source_id: UUID, since: datetime) -> None:
    """Soft-delete memories created during a failed extraction run and remove from ES."""
    from forum_memory.models.memory import Memory
    from forum_memory.services import es_service

    stmt = select(Memory).where(
        Memory.source_id == source_id,
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
        logger.info("Rolled back %d partial memories for source %s", len(memories), source_id)


def _create_record(session: Session, ctx: SourceContext, prev_retry: int = 0) -> ExtractionRecord:
    record = ExtractionRecord(
        source_type=ctx.source_type,
        source_id=ctx.source_id,
        namespace_id=ctx.namespace_id,
        status=ExtractionStatus.IN_PROGRESS,
        retry_count=prev_retry + 1 if prev_retry > 0 else 0,
    )
    session.add(record)
    session.commit()
    return record


def _execute_pipeline(session: Session, ctx: SourceContext, record: ExtractionRecord) -> list[UUID]:
    """Compress → extract → AUDN → persist."""
    llm = get_provider()
    compressed = maybe_compress(llm, ctx.title, ctx.question, ctx.discussion)
    facts = extract_facts(llm, ctx.title, ctx.question, compressed)

    memory_ids: list[UUID] = []
    batch_created: list[dict] = []

    for fact in facts:
        mid, _action = process_one_fact(session, llm, ctx, fact, batch_created)
        if mid:
            memory_ids.append(mid)
            batch_created.append({
                "id": str(mid),
                "content": fact["content"],
                "authority": ctx.authority.value,
                "tags": fact.get("tags", []),
                "knowledge_type": fact.get("knowledge_type"),
            })

    return memory_ids


def maybe_compress(llm, title: str, question: str, discussion: str) -> str:
    if len(discussion) < 3000:
        return discussion
    msgs = build_compress_messages(title, question, discussion)
    return llm.complete(msgs)


def extract_facts(llm, title: str, question: str, discussion: str) -> list[dict]:
    """Three-stage extraction: Structure → Atomize → Gate."""
    structured = stage_structure(llm, title, question, discussion)
    if not structured:
        logger.warning("Stage 1 (Structure) returned no result for '%s'", title)
        return []

    atoms = stage_atomize(llm, structured)
    if not atoms:
        logger.warning("Stage 2 (Atomize) produced no knowledge points for '%s'", title)
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
        logger.info("Stage 3 parse returned empty, retrying once — raw output: %s", raw[:300])
        raw = llm.complete(msgs)
        facts = parse_gated_facts(raw)
    logger.debug("Stage 3 (Gate): %d/%d atoms passed", len(facts), len(atoms))
    return facts


def process_one_fact(
    session, llm, ctx: SourceContext, fact: dict,
    batch_created: list[dict] | None = None,
) -> tuple[UUID | None, str]:
    """Run AUDN for a single fact and persist the result."""
    similar = find_similar(
        session, ctx.namespace_id, fact["content"], top_k=15,
        tags=fact.get("tags"), knowledge_type=fact.get("knowledge_type"),
    )
    if batch_created:
        seen_ids = {m["id"] for m in similar}
        for bc in batch_created:
            if bc["id"] not in seen_ids:
                similar.append(bc)

    msgs = build_audn_messages(fact["content"], similar)
    raw = llm.complete(msgs)
    result = parse_audn_response(raw)

    result = _retry_audn_if_needed(llm, msgs, result, ctx)
    result = _validate_audn_target(result, similar, ctx)

    data = _build_memory_create(ctx, fact)
    memory = apply_audn(session, data, result)
    return (memory.id if memory else None, result.action.value)


def _retry_audn_if_needed(llm, msgs, result: AUDNResult, ctx: SourceContext) -> AUDNResult:
    """Retry AUDN once if parse failed; flag for human review on second failure."""
    if "parse_error" not in result.reason:
        return result
    logger.info("AUDN parse failed for source %s, retrying once...", ctx.source_id)
    raw = llm.complete(msgs)
    return parse_audn_response(raw)


def _validate_audn_target(result: AUDNResult, similar: list[dict], ctx: SourceContext) -> AUDNResult:
    """Validate target_id is in the candidate list; fall back to ADD if not."""
    if not result.target_id:
        return result
    if result.action not in (AUDNAction.UPDATE, AUDNAction.DELETE):
        return result
    valid_ids = {m["id"] for m in similar}
    if result.target_id in valid_ids:
        return result
    logger.warning(
        "AUDN target_id %s not in candidate list (%d candidates), falling back to ADD",
        result.target_id, len(valid_ids),
    )
    return AUDNResult(action=AUDNAction.ADD, reason="target_id_not_in_candidates")


def _build_memory_create(ctx: SourceContext, fact: dict) -> MemoryCreate:
    """Build a MemoryCreate from SourceContext and a fact dict."""
    return MemoryCreate(
        namespace_id=ctx.namespace_id,
        content=fact["content"],
        knowledge_type=fact.get("knowledge_type"),
        tags=fact.get("tags"),
        environment=ctx.environment,
        source_type=ctx.source_type,
        source_id=ctx.source_id,
        source_role=ctx.source_role,
        resolved_type=ctx.resolved_type,
        authority=ctx.authority.value,
        pending_human_confirm=ctx.pending_human_confirm,
    )
