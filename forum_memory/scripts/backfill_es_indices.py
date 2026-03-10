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
from forum_memory.config import get_settings
from forum_memory.providers import get_provider
from forum_memory.services.es_service import ensure_index_by_name, bulk_reindex
from forum_memory.services.namespace_service import _generate_es_index_name

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BATCH_SIZE = 50


def main():
    settings = get_settings()
    provider = get_provider()

    with Session(engine) as session:
        # Find namespaces without es_index_name
        stmt = select(Namespace).where(
            Namespace.is_active == True,  # noqa: E712
            Namespace.es_index_name == None,  # noqa: E711
        )
        namespaces = list(session.exec(stmt).all())

        if not namespaces:
            logger.info("All namespaces already have ES index names. Nothing to backfill.")
            return

        logger.info("Found %d namespaces to backfill", len(namespaces))

        for ns in namespaces:
            # Generate ES-safe index name using UUID (always lowercase ASCII)
            index_name = _generate_es_index_name()
            logger.info("Backfilling namespace '%s' -> ES index '%s'",
                        ns.display_name, index_name)

            # Update DB
            ns.es_index_name = index_name
            session.commit()

            # Create ES index
            try:
                ensure_index_by_name(index_name)
            except Exception:
                logger.exception("Failed to create ES index %s, skipping", index_name)
                continue

            # Find and re-index memories for this namespace
            mem_stmt = select(Memory).where(
                Memory.namespace_id == ns.id,
                Memory.status == MemoryStatus.ACTIVE,
            )
            memories = list(session.exec(mem_stmt).all())
            total = len(memories)
            logger.info("  Found %d active memories to re-index", total)

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

    logger.info("Backfill complete")


if __name__ == "__main__":
    main()
