"""回填 board_follow 记录：为所有已有成员/管理员补建关注记录。

用法:
    python -m forum_memory.scripts.backfill_follows [--dry-run]
"""

import logging
import sys

from sqlmodel import Session, select

from forum_memory.database import engine
from forum_memory.models.namespace_moderator import NamespaceModerator
from forum_memory.models.board_follow import BoardFollow

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s")
logger = logging.getLogger(__name__)


def backfill(dry_run: bool = False) -> int:
    """Create BoardFollow for every membership that lacks one. Returns count."""
    with Session(engine) as session:
        members = session.exec(select(NamespaceModerator)).all()
        created = 0
        for mem in members:
            existing = session.exec(
                select(BoardFollow).where(
                    BoardFollow.user_id == mem.user_id,
                    BoardFollow.namespace_id == mem.namespace_id,
                )
            ).first()
            if existing:
                continue
            if not dry_run:
                session.add(BoardFollow(user_id=mem.user_id, namespace_id=mem.namespace_id))
            created += 1
            logger.info("Follow: user=%s  ns=%s", mem.user_id, mem.namespace_id)

        if not dry_run:
            session.commit()
        logger.info("%s %d follow records", "Would create" if dry_run else "Created", created)
        return created


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    if dry:
        logger.info("DRY RUN mode")
    backfill(dry_run=dry)
