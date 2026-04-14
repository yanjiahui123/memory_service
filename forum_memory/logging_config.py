"""Centralized logging configuration — categorized file output with rotation.

Log files:
  app.log         — catch-all (all INFO+ messages)
  error.log       — ERROR+ only (quick diagnosis)
  access.log      — API request/response (method, path, status, duration)
  extraction.log  — knowledge extraction pipeline
  scheduler.log   — background jobs and scheduled tasks

All logs also go to console (can be disabled via settings).
"""

import logging
import logging.handlers
from pathlib import Path

# ---------------------------------------------------------------------------
# Logger name constants (used by middleware & routing)
# ---------------------------------------------------------------------------

ACCESS_LOGGER = "forum_memory.access"

# Extraction pipeline logger names — these write to extraction.log
_EXTRACTION_LOGGERS = (
    "forum_memory.services.extraction_service",
    "forum_memory.core.extraction",
    "forum_memory.core.audn",
    "forum_memory.core.image_preprocessor",
    "forum_memory.core.quality",
)

# ---------------------------------------------------------------------------
# Formatter definitions
# ---------------------------------------------------------------------------

_DEFAULT_FMT = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
_DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"

_ACCESS_FMT = '%(asctime)s %(message)s'

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def setup_logging(
    log_dir: str = "logs",
    log_level: str = "INFO",
    console: bool = True,
) -> None:
    """Configure categorized file logging with rotation.

    Call once at application startup, BEFORE any logger is used.
    Replaces the default ``logging.basicConfig()`` call.
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, log_level.upper(), logging.INFO)

    # Build all handlers
    handlers = _build_handlers(log_path, level, console)

    # Configure root logger (catch-all)
    _configure_root(level, handlers)

    # Configure category loggers
    _configure_access_logger(level, handlers)
    _configure_extraction_loggers(handlers)
    _configure_scheduler_logger(handlers)

    # Suppress noisy third-party loggers
    _quiet_third_party()

    logging.getLogger(__name__).info(
        "Logging configured: dir=%s, level=%s, "
        "files=[app, error, access, extraction, scheduler]",
        log_path.resolve(), log_level,
    )


# ---------------------------------------------------------------------------
# Handler factory
# ---------------------------------------------------------------------------


def _make_rotating(
    filepath: Path,
    level: int,
    fmt: str = _DEFAULT_FMT,
    max_bytes: int = 20 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Handler:
    """Create a RotatingFileHandler with given params."""
    handler = logging.handlers.RotatingFileHandler(
        filepath,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter(fmt, datefmt=_DEFAULT_DATEFMT),
    )
    return handler


def _build_handlers(
    log_path: Path, level: int, console: bool,
) -> dict[str, logging.Handler]:
    """Build all named handlers."""
    result: dict[str, logging.Handler] = {}

    # Console (for dev / docker stdout)
    if console:
        ch = logging.StreamHandler()
        ch.setLevel(level)
        ch.setFormatter(
            logging.Formatter(_DEFAULT_FMT, datefmt=_DEFAULT_DATEFMT),
        )
        result["console"] = ch

    # app.log — everything INFO+
    result["app"] = _make_rotating(log_path / "app.log", level)

    # error.log — ERROR+ only, quick diagnosis
    result["error"] = _make_rotating(
        log_path / "error.log", logging.ERROR, max_bytes=10 * 1024 * 1024,
    )

    # access.log — API requests
    result["access"] = _make_rotating(
        log_path / "access.log", logging.INFO, fmt=_ACCESS_FMT,
    )

    # extraction.log — extraction pipeline
    result["extraction"] = _make_rotating(
        log_path / "extraction.log", level,
    )

    # scheduler.log — background jobs
    result["scheduler"] = _make_rotating(
        log_path / "scheduler.log", level, max_bytes=10 * 1024 * 1024,
    )

    return result


# ---------------------------------------------------------------------------
# Logger configuration helpers
# ---------------------------------------------------------------------------


def _configure_root(level: int, handlers: dict) -> None:
    """Root logger: console + app.log + error.log."""
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    for key in ("console", "app", "error"):
        if key in handlers:
            root.addHandler(handlers[key])


def _configure_access_logger(level: int, handlers: dict) -> None:
    """Access logger: access.log only + error.log, propagate=False to skip app.log.

    API access entries are high-volume; keeping them out of app.log
    avoids noise and keeps app.log focused on business logic.
    """
    lg = logging.getLogger(ACCESS_LOGGER)
    lg.setLevel(level)
    lg.propagate = False

    lg.addHandler(handlers["access"])
    if "console" in handlers:
        lg.addHandler(handlers["console"])
    lg.addHandler(handlers["error"])


def _configure_extraction_loggers(handlers: dict) -> None:
    """Extraction loggers: add extraction.log handler (still propagate to root)."""
    for name in _EXTRACTION_LOGGERS:
        logging.getLogger(name).addHandler(handlers["extraction"])


def _configure_scheduler_logger(handlers: dict) -> None:
    """Scheduler logger: add scheduler.log handler (still propagate to root)."""
    logging.getLogger("forum_memory.scheduler").addHandler(
        handlers["scheduler"],
    )


def _quiet_third_party() -> None:
    """Reduce noise from chatty libraries."""
    for name in ("urllib3", "elasticsearch", "apscheduler.scheduler",
                 "apscheduler.executors", "obs"):
        logging.getLogger(name).setLevel(logging.WARNING)
