"""Bulk re-index all active memories from PG to ES (per-namespace).

For each namespace with an es_index_name, re-indexes its memories into the
namespace-specific ES index. Falls back to the global index for namespaces
without es_index_name.

Usage: python -m forum_memory.scripts.reindex_memories
"""

import logging

from sqlmodel import Session, select

from forum_memory.database import engine
from forum_memory.models.memory import Memory
from forum_memory.models.namespace import Namespace
from forum_memory.models.enums import MemoryStatus
from forum_memory.providers import get_provider
from forum_memory.services.es_service import ensure_index, ensure_index_by_name, bulk_reindex

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BATCH_SIZE = 50


def _load_active_namespaces(session: Session) -> list[Namespace]:
    """Load all active namespaces."""
    stmt = select(Namespace).where(Namespace.is_active.is_(True))
    return list(session.exec(stmt).all())


def _load_active_memories(session: Session, ns: Namespace) -> list[Memory]:
    """Load all ACTIVE memories for a namespace."""
    stmt = select(Memory).where(
        Memory.namespace_id == ns.id,
        Memory.status == MemoryStatus.ACTIVE,
    )
    return list(session.exec(stmt).all())


def _ensure_namespace_index(ns: Namespace) -> str | None:
    """Ensure ES index exists for namespace. Returns index_name or None."""
    index_name = ns.es_index_name
    if not index_name:
        return None
    logger.info("Ensuring ES index '%s' for namespace '%s'", index_name, ns.name)
    try:
        ensure_index_by_name(index_name)
    except Exception:
        logger.exception("Failed to create ES index %s, using default", index_name)
        return None
    return index_name


def _build_index_doc(mem: Memory, embedding: list[float]) -> dict:
    """Build a single ES document from a memory and its embedding."""
    return {
        "memory_id": str(mem.id),
        "namespace_id": str(mem.namespace_id),
        "content": mem.content,
        "embedding": embedding,
        "status": mem.status,
        "environment": mem.environment,
        "tags": mem.tags,
        "knowledge_type": mem.knowledge_type,
        "quality_score": mem.quality_score,
    }


def _reindex_memories(memories: list[Memory], index_name: str | None, provider) -> int:
    """Embed and bulk-index memories in batches. Returns total indexed count."""
    total = len(memories)
    indexed = 0

    for i in range(0, total, BATCH_SIZE):
        batch = memories[i:i + BATCH_SIZE]
        try:
            embeddings = provider.embed_batch([m.content for m in batch])
        except Exception:
            logger.exception("  Embedding batch %d failed, skipping", i // BATCH_SIZE)
            continue

        docs = [_build_index_doc(m, emb) for m, emb in zip(batch, embeddings)]
        success, _failed_ids = bulk_reindex(docs, batch_size=BATCH_SIZE, index_name=index_name)
        indexed += success
        logger.info("  Batch %d: indexed %d/%d (total: %d/%d)",
                    i // BATCH_SIZE, success, len(batch), indexed, total)

    return indexed


def _reindex_namespace(session: Session, ns: Namespace, provider) -> None:
    """Re-index all active memories for a single namespace."""
    index_name = _ensure_namespace_index(ns)

    memories = _load_active_memories(session, ns)
    if not memories:
        logger.info("  Namespace '%s': no active memories, skipping", ns.name)
        return

    logger.info("  Namespace '%s': %d active memories to reindex", ns.name, len(memories))
    indexed = _reindex_memories(memories, index_name, provider)
    logger.info("  Namespace '%s' complete: %d/%d memories indexed",
                ns.name, indexed, len(memories))


def main():
    logger.info("Ensuring default ES index exists...")
    ensure_index()

    provider = get_provider()

    with Session(engine) as session:
        namespaces = _load_active_namespaces(session)
        for ns in namespaces:
            _reindex_namespace(session, ns, provider)

    logger.info("Reindex complete")


if __name__ == "__main__":
    main()
