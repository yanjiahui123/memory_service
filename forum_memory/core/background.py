"""Background task executor using ThreadPoolExecutor.

Provides fire-and-forget execution for tasks like AI answer generation,
so HTTP requests return immediately without blocking on LLM calls.
Each background task gets its own DB session (request session is closed by then).
"""

import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_executor: ThreadPoolExecutor | None = None


def init_executor(max_workers: int = 4) -> None:
    """Initialize the global thread pool. Called during app startup."""
    global _executor
    _executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="bg")
    logger.info("Background executor initialized (max_workers=%d)", max_workers)


def shutdown_executor() -> None:
    """Gracefully shut down the thread pool. Called during app shutdown."""
    global _executor
    if _executor:
        _executor.shutdown(wait=True, cancel_futures=False)
        logger.info("Background executor shut down")
        _executor = None


def submit(fn, *args, **kwargs):
    """Submit a task to the background thread pool.

    The callable should manage its own DB session and error handling.
    Errors are logged but do not propagate.
    """
    if _executor is None:
        logger.warning("Background executor not initialized, running task synchronously")
        try:
            fn(*args, **kwargs)
        except Exception:
            logger.exception("Background task failed (sync fallback)")
        return

    def _wrapper():
        try:
            fn(*args, **kwargs)
        except Exception:
            logger.exception("Background task failed")

    _executor.submit(_wrapper)
