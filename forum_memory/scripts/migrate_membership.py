"""Migration: Add membership management tables and columns.

Run once against the production database. Idempotent — safe to re-run.

Usage:
    python -m forum_memory.scripts.migrate_membership
"""

import logging

from sqlmodel import Session, text

from forum_memory.database import engine

logger = logging.getLogger(__name__)

STATEMENTS = [
    # 1. Add role column to namespace_moderators (default 'moderator' for backward compat)
    """
    ALTER TABLE memo_namespace_moderators
    ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'moderator';
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_memo_ns_mod_role
    ON memo_namespace_moderators(role);
    """,
    # 2. Add department columns to users
    """
    ALTER TABLE memo_users
    ADD COLUMN IF NOT EXISTS dept_code VARCHAR(50);
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_memo_users_dept_code
    ON memo_users(dept_code);
    """,
    """
    ALTER TABLE memo_users
    ADD COLUMN IF NOT EXISTS dept_path VARCHAR(500);
    """,
    """
    ALTER TABLE memo_users
    ADD COLUMN IF NOT EXISTS dept_levels JSONB;
    """,
    # 3. Create invites table
    """
    CREATE TABLE IF NOT EXISTS memo_namespace_invites (
        id UUID PRIMARY KEY,
        namespace_id UUID NOT NULL REFERENCES memo_namespaces(id),
        created_by UUID NOT NULL REFERENCES memo_users(id),
        code VARCHAR(32) NOT NULL UNIQUE,
        role VARCHAR(20) NOT NULL DEFAULT 'member',
        max_uses INTEGER,
        use_count INTEGER NOT NULL DEFAULT 0,
        expires_at TIMESTAMP WITH TIME ZONE,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_memo_ns_invites_code
    ON memo_namespace_invites(code);
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_memo_ns_invites_ns_id
    ON memo_namespace_invites(namespace_id);
    """,
    # 4. Create board follows table
    """
    CREATE TABLE IF NOT EXISTS memo_user_board_follows (
        id UUID PRIMARY KEY,
        user_id UUID NOT NULL REFERENCES memo_users(id),
        namespace_id UUID NOT NULL REFERENCES memo_namespaces(id),
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT uq_user_board_follow UNIQUE (user_id, namespace_id)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_memo_board_follows_user
    ON memo_user_board_follows(user_id);
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_memo_board_follows_ns
    ON memo_user_board_follows(namespace_id);
    """,
]


def run_migration() -> None:
    with Session(engine) as session:
        for stmt in STATEMENTS:
            logger.info("Executing: %s", stmt.strip()[:80])
            session.exec(text(stmt))
        session.commit()
    logger.info("Migration completed successfully.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_migration()
