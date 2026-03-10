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


def main():
    # Ensure default fallback index
    logger.info("Ensuring default ES index exists...")
    ensure_index()

    provider = get_provider()

    with Session(engine) as session:
        # Get all active namespaces
        namespaces = list(session.exec(
            select(Namespace).where(Namespace.is_active == True)  # noqa: E712
        ).all())

        for ns in namespaces:
            index_name = ns.es_index_name
            if index_name:
                logger.info("Ensuring ES index '%s' for namespace '%s'", index_name, ns.name)
                try:
                    ensure_index_by_name(index_name)
                except Exception:
                    logger.exception("Failed to create ES index %s, using default", index_name)
                    index_name = None

            stmt = select(Memory).where(
                Memory.namespace_id == ns.id,
                Memory.status == MemoryStatus.ACTIVE,
            )
            memories = list(session.exec(stmt).all())
            total = len(memories)

            if total == 0:
                logger.info("  Namespace '%s': no active memories, skipping", ns.name)
                continue

            logger.info("  Namespace '%s': %d active memories to reindex", ns.name, total)

            indexed = 0
            for i in range(0, total, BATCH_SIZE):
                batch = memories[i:i + BATCH_SIZE]
                texts = [m.content for m in batch]

                try:
                    embeddings = provider.embed_batch(texts)
                except Exception:
                    logger.exception("  Embedding batch %d failed, skipping", i // BATCH_SIZE)
                    continue

                docs = []
                for m, emb in zip(batch, embeddings):
                    docs.append({
                        "memory_id": str(m.id),
                        "namespace_id": str(m.namespace_id),
                        "content": m.content,
                        "embedding": emb,
                        "status": m.status,
                        "environment": m.environment,
                        "tags": m.tags,
                        "knowledge_type": m.knowledge_type,
                        "quality_score": m.quality_score,
                    })

                count = bulk_reindex(docs, batch_size=BATCH_SIZE, index_name=index_name)
                indexed += count
                logger.info("  Batch %d: indexed %d/%d (total: %d/%d)",
                            i // BATCH_SIZE, count, len(batch), indexed, total)

            logger.info("  Namespace '%s' complete: %d/%d memories indexed", ns.name, indexed, total)

    logger.info("Reindex complete")


if __name__ == "__main__":
    main()
