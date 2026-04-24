"""Add pending_reason column to memo_memories.

Idempotent: checks information_schema before ALTER TABLE.

Usage:
    python -m forum_memory.scripts.add_pending_reason_column [--dry-run]
"""

import argparse
import logging

from sqlalchemy import text as sa_text
from sqlmodel import Session

from forum_memory.database import engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s")
logger = logging.getLogger(__name__)


def _column_exists(session: Session) -> bool:
    check = sa_text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'memo_memories' AND column_name = 'pending_reason'"
    )
    return session.execute(check).first() is not None


def migrate(dry_run: bool = False) -> None:
    with Session(engine) as session:
        if _column_exists(session):
            logger.info("Column pending_reason already exists — nothing to do.")
            return
        if dry_run:
            logger.info("[DRY RUN] Would ALTER TABLE memo_memories ADD COLUMN pending_reason VARCHAR(50) NULL")
            return
        session.execute(sa_text(
            "ALTER TABLE memo_memories ADD COLUMN pending_reason VARCHAR(50) NULL"
        ))
        session.commit()
        logger.info("Added pending_reason column to memo_memories.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Add pending_reason column to memo_memories")
    parser.add_argument("--dry-run", action="store_true", help="演练模式，不写入数据库")
    args = parser.parse_args()
    migrate(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
