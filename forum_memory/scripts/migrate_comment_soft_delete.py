"""Migration: add deleted_at column to comments table for soft-delete support.

Usage: python -m forum_memory.scripts.migrate_comment_soft_delete

This script is idempotent — safe to run multiple times.
"""

import logging

from sqlalchemy import text

from forum_memory.database import engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MIGRATION_SQL = [
    # Add deleted_at column (nullable TIMESTAMP) to comments if it doesn't exist
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'comments' AND column_name = 'deleted_at'
        ) THEN
            ALTER TABLE comments ADD COLUMN deleted_at TIMESTAMP;
            CREATE INDEX ix_comments_deleted_at ON comments (deleted_at);
        END IF;
    END
    $$;
    """,
]


def main():
    logger.info("Running comment soft-delete migration...")
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
