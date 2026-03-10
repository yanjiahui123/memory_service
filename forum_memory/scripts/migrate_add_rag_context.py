"""Migration: add rag_context column to comments table.

Usage: python -m forum_memory.scripts.migrate_add_rag_context

This script is idempotent — safe to run multiple times.
"""

import logging

from sqlalchemy import text

from forum_memory.database import engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MIGRATION_SQL = [
    # Add rag_context column (nullable TEXT) to comments if it doesn't exist
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'comments' AND column_name = 'rag_context'
        ) THEN
            ALTER TABLE comments ADD COLUMN rag_context TEXT;
        END IF;
    END
    $$;
    """,
]


def main():
    logger.info("Running rag_context migration...")
    with engine.connect() as conn:
        for i, sql in enumerate(MIGRATION_SQL, 1):
            try:
                conn.execute(text(sql))
                conn.commit()
                logger.info("Step %d: OK", i)
            except Exception as e:
                logger.error("Step %d failed: %s", i, e)
                conn.rollback()
    logger.info("Migration complete")


if __name__ == "__main__":
    main()
