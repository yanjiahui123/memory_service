"""回填 is_imported 字段：将已有的通过 _src: tag 标记的导入帖子设为 is_imported=True。

用法:
    python -m forum_memory.scripts.backfill_is_imported [--dry-run]
"""

import logging

from sqlalchemy import String, func
from sqlmodel import Session, select

from forum_memory.database import engine
from forum_memory.models.enums import ThreadStatus
from forum_memory.models.thread import Thread

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s")
logger = logging.getLogger(__name__)

_SRC_TAG_PREFIX = "_src:"


def backfill(dry_run: bool = False) -> int:
    """Set is_imported=True for threads whose tags contain '_src:'. Returns count."""
    with Session(engine) as session:
        stmt = (
            select(Thread)
            .where(Thread.status != ThreadStatus.DELETED)
            .where(Thread.is_imported.is_(False))
            .where(func.cast(Thread.tags, String).contains(_SRC_TAG_PREFIX))
        )
        threads = list(session.exec(stmt).all())

        if not threads:
            logger.info("No threads to backfill.")
            return 0

        logger.info("Found %d imported threads to backfill%s", len(threads), " [DRY RUN]" if dry_run else "")
        for t in threads:
            logger.info("  → %s  '%s'", t.id, t.title[:60])
            t.is_imported = True

        if not dry_run:
            session.commit()
            logger.info("Backfill committed.")
        else:
            logger.info("Dry run — no changes written.")

        return len(threads)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="回填 is_imported 字段")
    parser.add_argument("--dry-run", action="store_true", help="演练模式，不写入数据库")
    args = parser.parse_args()
    count = backfill(dry_run=args.dry_run)
    print(f"\n回填完成: {count} 条帖子")


if __name__ == "__main__":
    main()
