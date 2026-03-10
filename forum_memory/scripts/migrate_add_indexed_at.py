"""Migration: add indexed_at column to memories table.

Usage: python -m forum_memory.scripts.migrate_add_indexed_at

This script is idempotent — safe to run multiple times.
The indexed_at column tracks DB-ES sync status:
  - NULL means ES index is pending or failed (needs repair)
  - A timestamp means ES is in sync as of that time
"""

import logging

from sqlalchemy import text

from forum_memory.database import engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MIGRATION_SQL = [
    # Step 1: Add indexed_at column (nullable TIMESTAMPTZ) if it doesn't exist
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'memories' AND column_name = 'indexed_at'
        ) THEN
            ALTER TABLE memories ADD COLUMN indexed_at TIMESTAMPTZ;
        END IF;
    END
    $$;
    """,
    # Step 2: Backfill existing ACTIVE memories — assume they are already indexed
    # (set indexed_at = updated_at for all ACTIVE memories where indexed_at IS NULL)
    """
    UPDATE memories
    SET indexed_at = updated_at
    WHERE status = 'ACTIVE' AND indexed_at IS NULL;
    """,
]


def main():
    logger.info("Running indexed_at migration...")
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
