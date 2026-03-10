"""Fix ES index names that contain uppercase or non-ASCII characters.

Regenerates es_index_name for any namespace whose current index name
is not ES-safe (lowercase ASCII only). Does NOT re-index data — run
backfill_es_indices.py afterwards for that.

Usage: python -m forum_memory.scripts.fix_es_index_names
"""

import re
import logging

from sqlmodel import Session, select

from forum_memory.database import engine
from forum_memory.models.namespace import Namespace
from forum_memory.services.namespace_service import _generate_es_index_name
from forum_memory.services.es_service import ensure_index_by_name

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ES_SAFE_PATTERN = re.compile(r'^[a-z0-9_\-\.]+$')


def main():
    with Session(engine) as session:
        namespaces = list(session.exec(
            select(Namespace).where(Namespace.is_active == True)
        ).all())

        fixed = 0
        for ns in namespaces:
            if ns.es_index_name and ES_SAFE_PATTERN.match(ns.es_index_name):
                continue

            old_name = ns.es_index_name
            new_name = _generate_es_index_name()
            ns.es_index_name = new_name
            session.commit()

            logger.info("Fixed namespace '%s': '%s' -> '%s'", ns.display_name, old_name, new_name)

            try:
                ensure_index_by_name(new_name)
            except Exception:
                logger.warning("Failed to create ES index %s (run backfill later)", new_name)

            fixed += 1

        logger.info("Done. Fixed %d / %d namespaces", fixed, len(namespaces))


if __name__ == "__main__":
    main()
