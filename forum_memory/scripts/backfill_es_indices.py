"""Backfill ES index names for existing namespaces and re-index memories.

For each namespace that doesn't have an es_index_name:
1. Generate a safe slug-based name (if current name is ES-incompatible)
2. Generate and store the ES index name
3. Create the ES index
4. Re-index all ACTIVE memories into the new per-namespace index

Usage: python -m forum_memory.scripts.backfill_es_indices
"""

import logging

from sqlmodel import Session, select

from forum_memory.database import engine
from forum_memory.models.namespace import Namespace
from forum_memory.models.memory import Memory
from forum_memory.models.enums import MemoryStatus
from forum_memory.providers import get_provider
from forum_memory.services.es_service import ensure_index_by_name, bulk_reindex
from forum_memory.services.namespace_service import _generate_es_index_name

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BATCH_SIZE = 50


def _find_namespaces_to_backfill(session: Session) -> list[Namespace]:
    """Find active namespaces that lack an ES index name."""
    stmt = select(Namespace).where(
        Namespace.is_active.is_(True),
        Namespace.es_index_name.is_(None),
    )
    return list(session.exec(stmt).all())


def _assign_index_name(session: Session, ns: Namespace) -> str:
    """Generate and persist ES index name for a namespace."""
    index_name = _generate_es_index_name()
    logger.info("Backfilling namespace '%s' -> ES index '%s'",
                ns.display_name, index_name)
    ns.es_index_name = index_name
    session.commit()
    return index_name


def _load_active_memories(session: Session, ns: Namespace) -> list[Memory]:
    """Load all ACTIVE memories for a namespace."""
    stmt = select(Memory).where(
        Memory.namespace_id == ns.id,
        Memory.status == MemoryStatus.ACTIVE,
    )
    return list(session.exec(stmt).all())


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


def _reindex_memories(memories: list[Memory], index_name: str, provider) -> int:
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


def _backfill_namespace(session: Session, ns: Namespace, provider) -> None:
    """Backfill a single namespace: assign index, create it, re-index memories."""
    index_name = _assign_index_name(session, ns)

    try:
        ensure_index_by_name(index_name)
    except Exception:
        logger.exception("Failed to create ES index %s, skipping", index_name)
        return

    memories = _load_active_memories(session, ns)
    logger.info("  Found %d active memories to re-index", len(memories))

    indexed = _reindex_memories(memories, index_name, provider)
    logger.info("  Namespace '%s' complete: %d/%d memories indexed",
                ns.name, indexed, len(memories))


def main():
    provider = get_provider()

    with Session(engine) as session:
        namespaces = _find_namespaces_to_backfill(session)
        if not namespaces:
            logger.info("All namespaces already have ES index names. Nothing to backfill.")
            return

        logger.info("Found %d namespaces to backfill", len(namespaces))
        for ns in namespaces:
            _backfill_namespace(session, ns, provider)

    logger.info("Backfill complete")


if __name__ == "__main__":
    main()
