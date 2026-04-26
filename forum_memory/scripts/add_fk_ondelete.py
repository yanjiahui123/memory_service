"""Add ON DELETE CASCADE / SET NULL to notification + feedback foreign keys.

Targets PostgreSQL. Idempotent — drops the old constraint by name (auto-generated
by SQLAlchemy convention) and re-creates it with the desired ON DELETE rule.

Usage:
    python -m forum_memory.scripts.add_fk_ondelete [--dry-run]
"""

import argparse
import logging

from sqlalchemy import text as sa_text
from sqlmodel import Session

from forum_memory.database import engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s")
logger = logging.getLogger(__name__)


# (table, column, ref_table, ref_column, on_delete)
TARGETS = [
    ("memo_notifications", "thread_id", "memo_threads", "id", "CASCADE"),
    ("memo_notifications", "comment_id", "memo_comments", "id", "CASCADE"),
    ("memo_feedbacks", "memory_id", "memo_memories", "id", "CASCADE"),
    ("memo_feedbacks", "user_id", "memo_users", "id", "SET NULL"),
]


def _find_constraint_name(session: Session, table: str, column: str) -> str | None:
    """Look up the FK constraint name for table.column via information_schema."""
    stmt = sa_text("""
        SELECT tc.constraint_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_name = :table
          AND kcu.column_name = :column
        LIMIT 1
    """)
    row = session.execute(stmt, {"table": table, "column": column}).first()
    return row[0] if row else None


def _current_on_delete(session: Session, constraint_name: str) -> str | None:
    stmt = sa_text("""
        SELECT delete_rule FROM information_schema.referential_constraints
        WHERE constraint_name = :name
    """)
    row = session.execute(stmt, {"name": constraint_name}).first()
    return row[0] if row else None


def migrate_one(
    session: Session, table: str, column: str, ref_table: str,
    ref_column: str, on_delete: str, dry_run: bool,
) -> None:
    name = _find_constraint_name(session, table, column)
    if not name:
        logger.warning("FK %s.%s not found — skipping", table, column)
        return
    current = _current_on_delete(session, name) or "NO ACTION"
    desired = on_delete.upper()
    if current.upper() == desired:
        logger.info("%s.%s already ON DELETE %s — nothing to do", table, column, desired)
        return
    logger.info(
        "%s.%s: %s → %s (constraint=%s)", table, column, current, desired, name,
    )
    if dry_run:
        return
    session.execute(sa_text(f'ALTER TABLE {table} DROP CONSTRAINT "{name}"'))
    session.execute(sa_text(
        f'ALTER TABLE {table} ADD CONSTRAINT "{name}" '
        f'FOREIGN KEY ({column}) REFERENCES {ref_table} ({ref_column}) '
        f'ON DELETE {desired}'
    ))


def migrate(dry_run: bool = False) -> None:
    with Session(engine) as session:
        for spec in TARGETS:
            migrate_one(session, *spec, dry_run=dry_run)
        if not dry_run:
            session.commit()
    logger.info("Done.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    migrate(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
