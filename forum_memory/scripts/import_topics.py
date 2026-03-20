"""批量导入历史帖子 JSON 文件脚本。

文件命名约定: {简要描述}_{topic_id}_topic.json
JSON 结构:
    {
        "topic_id": <int>,
        "question": <str>,
        "topic_user_name": <str>,
        "best_answer_url": <str|null>,
        "reply_posts": [
            {
                "user_name": <str>,
                "topic_closed": <bool>,
                "is_solution": <bool>,
                "post_url": <str>,
                "text": <str>
            }
        ]
    }

用法:
    python -m forum_memory.scripts.import_topics \\
        --dir /path/to/json/files \\
        --namespace-id <uuid> \\
        [--workers 4] \\
        [--skip-extraction] \\
        [--dry-run]
"""

import argparse
import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from sqlmodel import Session, select

from forum_memory.database import engine
from forum_memory.models.enums import ResolvedType, SystemRole, ThreadStatus
from forum_memory.models.namespace import Namespace
from forum_memory.models.thread import Comment, Thread
from forum_memory.models.user import User

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Tag prefix for deduplication — stored in Thread.tags
_SRC_TAG_PREFIX = "_src:"


# ─── Helpers ────────────────────────────────────────────────────────────────

def _src_tag(topic_id) -> str:
    return f"{_SRC_TAG_PREFIX}{topic_id}"


def _parse_filename(filename: str) -> tuple[str, str | None]:
    """Parse '{description}_{topic_id}_topic.json'.

    Returns (title, topic_id_str).
    Falls back to stem as title if pattern doesn't match.
    """
    stem = Path(filename).stem
    if stem.endswith("_topic"):
        stem = stem[:-6]
    m = re.match(r"^(.+?)_(\d+)$", stem)
    if m:
        raw_desc, topic_id = m.group(1), m.group(2)
    else:
        raw_desc, topic_id = stem, None
    title = raw_desc.replace("_", " ").strip()
    return title, topic_id


def _get_or_create_user(session: Session, username: str) -> User:
    """Idempotently find or create a placeholder imported user."""
    existing = session.exec(select(User).where(User.username == username)).first()
    if existing:
        return existing

    h = int(hashlib.sha1(username.encode()).hexdigest(), 16) % 10_000_000
    employee_id = f"I{h:07d}"

    if session.exec(select(User).where(User.employee_id == employee_id)).first():
        employee_id = f"J{h:07d}"

    user = User(
        employee_id=employee_id,
        username=username,
        display_name=username,
        email=f"{username}@import.local",
        role=SystemRole.USER,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _already_imported(session: Session, namespace_id: UUID, topic_id: str) -> bool:
    """Check if a topic was already imported by inspecting Thread.tags."""
    tag = _src_tag(topic_id)
    from sqlalchemy import String, func
    stmt = (
        select(Thread)
        .where(Thread.namespace_id == namespace_id)
        .where(Thread.status != ThreadStatus.DELETED)
        .where(func.cast(Thread.tags, String).contains(tag))
    )
    return session.exec(stmt).first() is not None


# ─── Thread creation helpers ───────────────────────────────────────────────

def _create_comments(session: Session, thread: Thread, reply_posts: list[dict]) -> tuple[UUID | None, bool]:
    """Create comments for a thread. Returns (best_answer_comment_id, topic_closed)."""
    best_answer_url = None  # will be set from thread data if needed
    best_answer_id: UUID | None = None
    topic_closed = False

    for reply in reply_posts:
        reply_user = _get_or_create_user(session, reply.get("user_name") or "unknown")
        comment = Comment(
            thread_id=thread.id,
            author_id=reply_user.id,
            content=reply.get("text") or "",
            is_ai=False,
            author_role="commenter",
        )
        session.add(comment)
        session.flush()
        thread.comment_count += 1

        if _is_best_answer(reply, best_answer_url, best_answer_id):
            best_answer_id = comment.id
        if reply.get("topic_closed", False):
            topic_closed = True

    return best_answer_id, topic_closed


def _has_best_answer(data: dict, reply_posts: list[dict]) -> bool:
    """Check whether the topic has any best-answer indicator."""
    if data.get("best_answer_url"):
        return True
    return any(r.get("is_solution") for r in reply_posts)


def _is_best_answer(reply: dict, best_answer_url: str | None, current_best: UUID | None) -> bool:
    """Determine if a reply is the best answer."""
    post_url = reply.get("post_url") or ""
    is_solution = bool(reply.get("is_solution", False))
    if best_answer_url and post_url and post_url == best_answer_url:
        return True
    if not best_answer_url and is_solution and current_best is None:
        return True
    return False


def _resolve_thread(session: Session, thread: Thread, best_answer_id: UUID | None, topic_closed: bool) -> bool:
    """Apply resolution status to thread. Returns True if resolved."""
    now = datetime.now(tz=timezone(timedelta(hours=8)))

    if best_answer_id:
        best_comment = session.get(Comment, best_answer_id)
        if best_comment:
            best_comment.is_best_answer = True
        thread.status = ThreadStatus.RESOLVED
        thread.resolved_type = ResolvedType.HUMAN_RESOLVED
        thread.best_answer_id = best_answer_id
        thread.resolved_at = now
        return True

    if topic_closed:
        thread.status = ThreadStatus.TIMEOUT_CLOSED
        thread.resolved_type = ResolvedType.TIMEOUT
        thread.timeout_at = now
        return True

    return False


# ─── Core import logic ──────────────────────────────────────────────────────

def _import_one_file(
    filepath: Path,
    namespace_id: UUID,
    dry_run: bool,
) -> tuple[UUID | None, bool, str]:
    """Import a single JSON topic file.

    Returns (thread_id | None, was_resolved, status_msg).
    """
    try:
        data = json.loads(filepath.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, False, f"JSON parse error: {exc}"

    title, topic_id = _parse_filename(filepath.name)
    reply_posts = data.get("reply_posts") or []

    if not _has_best_answer(data, reply_posts):
        logger.debug("skip %-50s  → no best answer", filepath.name)
        return None, False, "skip (no best answer)"

    if dry_run:
        return _dry_run_result(title, data, reply_posts)

    return _persist_topic(filepath, namespace_id, title, topic_id, data, reply_posts)


def _dry_run_result(title: str, data: dict, reply_posts: list) -> tuple[None, bool, str]:
    """Build dry-run status message without writing to DB."""
    has_solution = data.get("best_answer_url") or any(r.get("is_solution") for r in reply_posts)
    hint = "✓ has_solution" if has_solution else "○ open"
    return None, False, f"[DRY RUN] '{title}' — {len(reply_posts)} replies  {hint}"


def _persist_topic(
    filepath: Path,
    namespace_id: UUID,
    title: str,
    topic_id: str | None,
    data: dict,
    reply_posts: list[dict],
) -> tuple[UUID | None, bool, str]:
    """Write thread + comments to DB. Returns (thread_id, was_resolved, msg)."""
    with Session(engine) as session:
        if topic_id and _already_imported(session, namespace_id, topic_id):
            return None, False, f"skip (already imported topic_id={topic_id})"

        author = _get_or_create_user(session, data.get("topic_user_name") or "unknown")
        thread_tags = [_src_tag(topic_id)] if topic_id else []

        thread = Thread(
            namespace_id=namespace_id,
            author_id=author.id,
            title=title,
            content=data.get("question") or "",
            status=ThreadStatus.OPEN,
            tags=thread_tags if thread_tags else None,
        )
        session.add(thread)
        session.flush()

        best_answer_id, topic_closed = _create_comments(session, thread, reply_posts)
        was_resolved = _resolve_thread(session, thread, best_answer_id, topic_closed)

        session.commit()
        logger.info("Imported %-60s  replies=%-3d  %s",
                    f"'{title}'", len(reply_posts),
                    "RESOLVED" if was_resolved else "OPEN")
        return thread.id, was_resolved, "ok"


# ─── Extraction worker ───────────────────────────────────────────────────────

def _extract_worker(thread_id: UUID) -> tuple[UUID, int, str]:
    """Run extraction pipeline for one thread."""
    import forum_memory.adapters  # noqa: F401  — ensure adapters registered
    from forum_memory.services.extraction_service import run_extraction

    try:
        with Session(engine) as session:
            mids = run_extraction(session, "thread", thread_id)
            return thread_id, len(mids), "ok"
    except Exception as exc:
        logger.warning("Extraction failed for thread %s: %s", thread_id, exc)
        return thread_id, 0, f"error: {exc}"


# ─── Phase runners ───────────────────────────────────────────────────────────

def _run_import_phase(files: list[Path], namespace_id: UUID, dry_run: bool) -> tuple[int, int, int, list[UUID]]:
    """Phase 1: import threads sequentially. Returns (imported, skipped, failed, resolved_ids)."""
    imported = skipped = failed = 0
    resolved_ids: list[UUID] = []

    for f in files:
        thread_id, was_resolved, msg = _import_one_file(f, namespace_id, dry_run)
        if dry_run:
            logger.info(msg)
            imported += 1
        elif thread_id is None:
            if msg.startswith("skip"):
                logger.debug("%-50s  → %s", f.name, msg)
                skipped += 1
            else:
                logger.error("FAIL %-50s  → %s", f.name, msg)
                failed += 1
        else:
            imported += 1
            if was_resolved:
                resolved_ids.append(thread_id)

    logger.info("Phase 1 done — imported=%d  skipped=%d  failed=%d  to_extract=%d",
                imported, skipped, failed, len(resolved_ids))
    return imported, skipped, failed, resolved_ids


def _run_extraction_phase(resolved_ids: list[UUID], workers: int) -> tuple[int, int]:
    """Phase 2: concurrent extraction. Returns (extracted, failed)."""
    actual_workers = min(workers, len(resolved_ids))
    logger.info("Phase 2 — extracting memories for %d threads  (workers=%d)",
                len(resolved_ids), actual_workers)

    extracted = extract_failed = 0
    with ThreadPoolExecutor(max_workers=actual_workers, thread_name_prefix="extractor") as pool:
        futures = {pool.submit(_extract_worker, tid): tid for tid in resolved_ids}
        for fut in as_completed(futures):
            tid, n_mems, status = fut.result()
            if status == "ok":
                logger.info("  ✓ thread=%s  memories=%d", tid, n_mems)
                extracted += 1
            else:
                logger.warning("  ✗ thread=%s  %s", tid, status)
                extract_failed += 1

    logger.info("Phase 2 done — extracted=%d  failed=%d", extracted, extract_failed)
    return extracted, extract_failed


def run_import(
    dir_path: Path,
    namespace_id: UUID,
    workers: int = 4,
    skip_extraction: bool = False,
    dry_run: bool = False,
) -> dict:
    """Orchestrate batch import: parse threads, then extract memories."""
    files = sorted(dir_path.glob("*.json"))
    if not files:
        logger.warning("No JSON files found in %s", dir_path)
        return {"total": 0, "imported": 0, "skipped": 0, "failed": 0,
                "resolved": 0, "extracted": 0, "extract_failed": 0}

    logger.info("Found %d JSON files  namespace=%s  workers=%d%s",
                len(files), namespace_id, workers,
                "  [DRY RUN]" if dry_run else "")

    imported, skipped, failed, resolved_ids = _run_import_phase(files, namespace_id, dry_run)

    extracted = extract_failed = 0
    if not skip_extraction and not dry_run and resolved_ids:
        extracted, extract_failed = _run_extraction_phase(resolved_ids, workers)

    return {
        "total": len(files),
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
        "resolved": len(resolved_ids),
        "extracted": extracted,
        "extract_failed": extract_failed,
    }


# ─── CLI entry point ─────────────────────────────────────────────────────────

_CLI_EPILOG = """
示例:
  # 导入全部 JSON，4 线程并发提取记忆
  python -m forum_memory.scripts.import_topics \\
      --dir ./history_topics \\
      --namespace-id xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

  # 仅导入帖子，跳过记忆提取（可稍后手动触发）
  python -m forum_memory.scripts.import_topics \\
      --dir ./history_topics \\
      --namespace-id xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx \\
      --skip-extraction

  # 演练模式（不写入数据库）
  python -m forum_memory.scripts.import_topics \\
      --dir ./history_topics \\
      --namespace-id xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx \\
      --dry-run
"""


def _parse_cli_args() -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    """Parse and validate CLI arguments. Returns (parser, args)."""
    parser = argparse.ArgumentParser(
        description="批量导入历史帖子 JSON 文件到 Forum Memory",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_CLI_EPILOG,
    )
    parser.add_argument("--dir", required=True, metavar="PATH", help="JSON 文件所在目录")
    parser.add_argument("--namespace-id", required=True, metavar="UUID", help="目标板块 UUID")
    parser.add_argument("--workers", type=int, default=4, metavar="N",
                        help="记忆提取并发线程数（默认 4）")
    parser.add_argument("--skip-extraction", action="store_true",
                        help="跳过记忆提取（仅导入帖子和回复）")
    parser.add_argument("--dry-run", action="store_true",
                        help="演练模式：解析文件但不写入数据库")
    return parser, parser.parse_args()


def _validate_inputs(parser: argparse.ArgumentParser, args: argparse.Namespace) -> tuple[Path, UUID]:
    """Validate directory and namespace UUID from CLI args."""
    dir_path = Path(args.dir)
    if not dir_path.is_dir():
        parser.error(f"目录不存在: {dir_path}")

    try:
        namespace_id = UUID(args.namespace_id)
    except ValueError:
        parser.error("--namespace-id 格式不正确，应为标准 UUID")

    if not args.dry_run:
        with Session(engine) as session:
            ns = session.get(Namespace, namespace_id)
            if not ns:
                parser.error(f"板块 {namespace_id} 不存在，请先创建板块")
            logger.info("目标板块: %s (%s)", ns.display_name or ns.name, ns.id)

    return dir_path, namespace_id


def _print_summary(stats: dict) -> None:
    """Print formatted import summary."""
    print("\n" + "═" * 40)
    print("  导入结果汇总")
    print("═" * 40)
    labels = {
        "total": "JSON 文件总数",
        "imported": "成功导入",
        "skipped": "跳过 (已导入)",
        "failed": "导入失败",
        "resolved": "已解决帖子",
        "extracted": "记忆提取成功",
        "extract_failed": "记忆提取失败",
    }
    for key, label in labels.items():
        print(f"  {label:<14}  {stats.get(key, 0)}")
    print("═" * 40)


def main() -> None:
    parser, args = _parse_cli_args()
    dir_path, namespace_id = _validate_inputs(parser, args)

    stats = run_import(
        dir_path=dir_path,
        namespace_id=namespace_id,
        workers=args.workers,
        skip_extraction=args.skip_extraction,
        dry_run=args.dry_run,
    )
    _print_summary(stats)


if __name__ == "__main__":
    main()
