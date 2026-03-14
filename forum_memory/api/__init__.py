"""API router registration."""

from fastapi import FastAPI

from forum_memory.api.namespaces import router as ns_router
from forum_memory.api.threads import router as thread_router
from forum_memory.api.memories import router as memory_router
from forum_memory.api.feedback import router as fb_router
from forum_memory.api.users import router as user_router
from forum_memory.api.uploads import router as upload_router
from forum_memory.api.admin import router as admin_router
from forum_memory.api.auth import router as auth_router
from forum_memory.api.relations import router as relation_router


def register_routers(app: FastAPI) -> None:
    prefix = "/api/v1"
    app.include_router(auth_router, prefix=prefix)
    app.include_router(user_router, prefix=prefix)
    app.include_router(ns_router, prefix=prefix)
    app.include_router(thread_router, prefix=prefix)
    app.include_router(memory_router, prefix=prefix)
    app.include_router(fb_router, prefix=prefix)
    app.include_router(upload_router, prefix=prefix)
    app.include_router(admin_router, prefix=prefix)
    app.include_router(relation_router, prefix=prefix)