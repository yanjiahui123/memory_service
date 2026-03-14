"""Migration: replace thread_id with (source_type, source_id) in extraction_records.

Usage: python -m forum_memory.scripts.migrate_extraction_source

This script is idempotent — safe to run multiple times.
It transitions the ExtractionRecord table from a Thread-specific FK
(thread_id) to generic source fields (source_type, source_id).
"""

import logging

from sqlalchemy import text

from forum_memory.database import engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MIGRATION_SQL = [
    # Step 1: Add source_type column if it doesn't exist
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'extraction_records' AND column_name = 'source_type'
        ) THEN
            ALTER TABLE extraction_records
            ADD COLUMN source_type VARCHAR(50) NOT NULL DEFAULT 'thread';
        END IF;
    END
    $$;
    """,
    # Step 2: Add source_id column if it doesn't exist
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'extraction_records' AND column_name = 'source_id'
        ) THEN
            ALTER TABLE extraction_records ADD COLUMN source_id UUID;
        END IF;
    END
    $$;
    """,
    # Step 3: Backfill source_id from thread_id where still NULL
    """
    UPDATE extraction_records
    SET source_id = thread_id
    WHERE source_id IS NULL AND thread_id IS NOT NULL;
    """,
    # Step 4: Drop old FK constraint on thread_id (name may vary)
    """
    DO $$
    DECLARE
        fk_name TEXT;
    BEGIN
        SELECT constraint_name INTO fk_name
        FROM information_schema.table_constraints
        WHERE table_name = 'extraction_records'
          AND constraint_type = 'FOREIGN KEY'
          AND constraint_name LIKE '%thread_id%'
        LIMIT 1;

        IF fk_name IS NOT NULL THEN
            EXECUTE 'ALTER TABLE extraction_records DROP CONSTRAINT ' || fk_name;
        END IF;
    END
    $$;
    """,
    # Step 5: Drop old unique constraint on thread_id
    """
    DO $$
    DECLARE
        uq_name TEXT;
    BEGIN
        SELECT constraint_name INTO uq_name
        FROM information_schema.table_constraints
        WHERE table_name = 'extraction_records'
          AND constraint_type = 'UNIQUE'
          AND constraint_name LIKE '%thread_id%'
        LIMIT 1;

        IF uq_name IS NOT NULL THEN
            EXECUTE 'ALTER TABLE extraction_records DROP CONSTRAINT ' || uq_name;
        END IF;
    END
    $$;
    """,
    # Step 6: Drop old index on thread_id if exists
    """
    DROP INDEX IF EXISTS ix_extraction_records_thread_id;
    """,
    # Step 7: Drop the thread_id column
    """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'extraction_records' AND column_name = 'thread_id'
        ) THEN
            ALTER TABLE extraction_records DROP COLUMN thread_id;
        END IF;
    END
    $$;
    """,
    # Step 8: Add composite unique constraint
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.table_constraints
            WHERE table_name = 'extraction_records'
              AND constraint_name = 'uq_extraction_source'
        ) THEN
            ALTER TABLE extraction_records
            ADD CONSTRAINT uq_extraction_source UNIQUE (source_type, source_id);
        END IF;
    END
    $$;
    """,
    # Step 9: Add index on source_type
    """
    CREATE INDEX IF NOT EXISTS ix_extraction_records_source_type
    ON extraction_records (source_type);
    """,
    # Step 10: Add index on source_id
    """
    CREATE INDEX IF NOT EXISTS ix_extraction_records_source_id
    ON extraction_records (source_id);
    """,
]


def main():
    logger.info("Running extraction_records source migration...")
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
