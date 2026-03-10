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
async def lifespan(app: FastAPI):
    """Startup: create tables, ES index, and background executor."""
    from forum_memory.core.background import init_executor, shutdown_executor

    try:
        init_db()
        logger.info("Database tables initialized successfully")
    except Exception as e:
        logger.error("Failed to initialize database: %s", e)
    # Initialize ES indices (non-fatal if ES unavailable)
    try:
        from forum_memory.services.es_service import ensure_index, ensure_index_by_name
        # Ensure default fallback index
        ensure_index()
        # Ensure per-namespace indices
        from sqlmodel import Session, select
        from forum_memory.database import engine as db_engine
        from forum_memory.models.namespace import Namespace
        with Session(db_engine) as session:
            namespaces = session.exec(
                select(Namespace).where(Namespace.is_active == True)
            ).all()
            for ns in namespaces:
                if ns.es_index_name:
                    try:
                        ensure_index_by_name(ns.es_index_name)
                    except Exception:
                        logger.warning("Failed to ensure ES index %s", ns.es_index_name)
        logger.info("Elasticsearch indices ensured")
    except Exception as e:
        logger.warning("Elasticsearch index creation failed (non-fatal): %s", e)

    init_executor(max_workers=4)
    yield
    shutdown_executor()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    _add_cors(app)
    register_routers(app)
    _mount_uploads(app, settings)
    _add_health_check(app)
    return app


def _add_cors(app: FastAPI) -> None:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Total-Count"],
    )


def _mount_uploads(app: FastAPI, settings) -> None:
    upload_path = Path(settings.upload_dir)
    upload_path.mkdir(parents=True, exist_ok=True)
    app.mount("/uploads", StaticFiles(directory=str(upload_path)), name="uploads")


def _add_health_check(app: FastAPI) -> None:
    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/v1/health/db")
    def health_db():
        """检查数据库连接"""
        from forum_memory.database import engine
        from sqlalchemy import text
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return {"status": "ok", "database": "connected"}
        except Exception as e:
            return {"status": "error", "database": str(e)}


# 模块级 app 实例：供 ASGI 服务器使用（uvicorn forum_memory.main:app）
app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)