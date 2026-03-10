"""Migration: add board_admin to systemrole enum + create namespace_moderators table.

Usage: python -m forum_memory.scripts.migrate_board_admin

This script is idempotent — safe to run multiple times.
"""

import logging

from sqlalchemy import text

from forum_memory.database import engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MIGRATION_SQL = [
    # 1. Add 'board_admin' to systemrole enum (PostgreSQL)
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_enum
            WHERE enumlabel = 'board_admin'
              AND enumtypid = (SELECT oid FROM pg_type WHERE typname = 'systemrole')
        ) THEN
            ALTER TYPE systemrole ADD VALUE 'board_admin';
        END IF;
    END
    $$;
    """,

    # 2. Create namespace_moderators table
    """
    CREATE TABLE IF NOT EXISTS namespace_moderators (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id UUID NOT NULL REFERENCES users(id),
        namespace_id UUID NOT NULL REFERENCES namespaces(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_user_namespace UNIQUE (user_id, namespace_id)
    );
    """,

    # 3. Create indexes
    "CREATE INDEX IF NOT EXISTS ix_namespace_moderators_user_id ON namespace_moderators(user_id);",
    "CREATE INDEX IF NOT EXISTS ix_namespace_moderators_namespace_id ON namespace_moderators(namespace_id);",
]


def main():
    logger.info("Running board_admin migration...")
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
