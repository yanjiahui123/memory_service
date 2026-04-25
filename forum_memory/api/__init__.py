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
from forum_memory.api.notifications import router as notif_router
from forum_memory.api.members import router as member_router
from forum_memory.api.share_links import router as share_link_router


def register_routers(app: FastAPI) -> None:
    app.include_router(auth_router)
    app.include_router(user_router)
    app.include_router(ns_router)
    app.include_router(thread_router)
    app.include_router(memory_router)
    app.include_router(fb_router)
    app.include_router(upload_router)
    app.include_router(admin_router)
    app.include_router(relation_router)
    app.include_router(notif_router)
    app.include_router(member_router)
    app.include_router(share_link_router)