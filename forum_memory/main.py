"""Forum Memory Agent — FastAPI application (synchronous)."""

import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from forum_memory.api.rate_limit import limiter
from forum_memory.config import get_settings
from forum_memory.database import init_db
from forum_memory.api import register_routers

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup: create tables, ES index, background executor, and scheduler."""
    from forum_memory.core.background import init_executor, shutdown_executor
    from forum_memory.scheduler.scheduler import init_scheduler, shutdown_scheduler

    # Register all source adapters
    import forum_memory.adapters  # noqa: F401

    try:
        init_db()
        logger.info("Database tables initialized successfully")
    except Exception as e:
        logger.error("Failed to initialize database: %s", e)
    # Initialize ES indices (non-fatal if ES unavailable)
    _ensure_es_indices()

    init_executor(max_workers=4)
    init_scheduler()
    yield
    shutdown_scheduler()
    shutdown_executor()


def _ensure_es_indices() -> None:
    """Create default and per-namespace ES indices (non-fatal on failure)."""
    try:
        from forum_memory.services.es_service import ensure_index, ensure_index_by_name
        ensure_index()
        _ensure_namespace_indices(ensure_index_by_name)
        logger.info("Elasticsearch indices ensured")
    except Exception as e:
        logger.warning("Elasticsearch index creation failed (non-fatal): %s", e)


def _ensure_namespace_indices(ensure_fn) -> None:
    """Ensure ES indices exist for all active namespaces."""
    from sqlmodel import Session, select
    from forum_memory.database import engine as db_engine
    from forum_memory.models.namespace import Namespace
    with Session(db_engine) as session:
        namespaces = session.exec(
            select(Namespace).where(Namespace.is_active.is_(True))
        ).all()
    for ns in namespaces:
        if ns.es_index_name:
            _ensure_single_index(ensure_fn, ns.es_index_name)


def _ensure_single_index(ensure_fn, index_name: str) -> None:
    """Try to ensure a single ES index, log warning on failure."""
    try:
        ensure_fn(index_name)
    except Exception:
        logger.warning("Failed to ensure ES index %s", index_name)


def create_app() -> FastAPI:
    settings = get_settings()
    fastapi_app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        lifespan=lifespan,
    )
    fastapi_app.state.limiter = limiter
    fastapi_app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    _add_cors(fastapi_app)
    register_routers(fastapi_app)
    _mount_uploads(fastapi_app, settings)
    _add_health_check(fastapi_app)
    return fastapi_app


def _add_cors(target_app: FastAPI) -> None:
    target_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Total-Count"],
    )


def _mount_uploads(target_app: FastAPI, settings) -> None:
    upload_path = Path(settings.upload_dir)
    upload_path.mkdir(parents=True, exist_ok=True)
    target_app.mount("/uploads", StaticFiles(directory=str(upload_path)), name="uploads")


def _add_health_check(target_app: FastAPI) -> None:
    @target_app.get("/health")
    def health():
        return {"status": "ok"}

    @target_app.get("/api/v1/health/db")
    def health_db():
        """检查数据库连接"""
        from forum_memory.database import engine as db_engine
        from sqlalchemy import text
        try:
            with db_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return {"status": "ok", "database": "connected"}
        except Exception as e:
            return {"status": "error", "database": str(e)}


# 模块级 app 实例：供 ASGI 服务器使用（uvicorn forum_memory.main:app）
app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
