"""Add gate_confidence column to memo_memories and recalculate quality scores.

Steps:
  1. ALTER TABLE: add gate_confidence column (FLOAT, default 0.5) if not exists
  2. Recalculate quality_score for all ACTIVE memories using the new 7-factor formula

Usage:
    python -m forum_memory.scripts.add_gate_confidence [--dry-run]
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text as sa_text
from sqlmodel import Session, select

from forum_memory.database import engine
from forum_memory.core.quality import compute_quality_score
from forum_memory.models.memory import Memory
from forum_memory.models.enums import MemoryStatus

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s")
logger = logging.getLogger(__name__)

BATCH_SIZE = 200


def _add_column_if_not_exists(session: Session) -> bool:
    """Add gate_confidence column to memo_memories. Returns True if added."""
    check = sa_text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'memo_memories' AND column_name = 'gate_confidence'"
    )
    exists = session.execute(check).first()
    if exists:
        logger.info("Column gate_confidence already exists, skipping ALTER TABLE.")
        return False

    session.execute(sa_text(
        "ALTER TABLE memo_memories ADD COLUMN gate_confidence FLOAT NOT NULL DEFAULT 0.5"
    ))
    session.commit()
    logger.info("Added gate_confidence column to memo_memories.")
    return True


def _recalc_quality_scores(session: Session, dry_run: bool) -> int:
    """Recalculate quality_score for all ACTIVE memories using the updated formula."""
    offset = 0
    total = 0
    while True:
        stmt = (
            select(Memory)
            .where(Memory.status == MemoryStatus.ACTIVE)
            .order_by(Memory.id)
            .offset(offset)
            .limit(BATCH_SIZE)
        )
        memories = list(session.exec(stmt).all())
        if not memories:
            break

        changed = 0
        for m in memories:
            old_score = m.quality_score
            new_score = compute_quality_score(
                useful=m.useful_count,
                not_useful=m.not_useful_count,
                wrong=m.wrong_count,
                outdated=m.outdated_count,
                source_role=m.source_role,
                retrieve_count=m.retrieve_count,
                created_at=m.created_at,
                cite_count=m.cite_count,
                resolved_citation_count=m.resolved_citation_count,
                gate_confidence=m.gate_confidence,
            )
            if abs(new_score - old_score) > 0.001:
                m.quality_score = new_score
                m.indexed_at = None  # Mark ES as stale for repair sensor
                changed += 1

        if changed and not dry_run:
            session.commit()
        total += changed
        offset += BATCH_SIZE
        logger.info("Batch offset=%d: %d/%d scores updated", offset, changed, len(memories))

    return total


def migrate(dry_run: bool = False) -> None:
    """Run migration: add column + recalculate scores."""
    with Session(engine) as session:
        if not dry_run:
            _add_column_if_not_exists(session)
        else:
            logger.info("[DRY RUN] Would add gate_confidence column")

        updated = _recalc_quality_scores(session, dry_run)
        suffix = " [DRY RUN]" if dry_run else ""
        logger.info("Quality score recalculation complete: %d memories updated%s", updated, suffix)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="添加 gate_confidence 列并重算质量分")
    parser.add_argument("--dry-run", action="store_true", help="演练模式，不写入数据库")
    args = parser.parse_args()
    migrate(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
