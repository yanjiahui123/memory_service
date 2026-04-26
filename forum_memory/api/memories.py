"""Memory API routes — sync."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlmodel import Session, select

from forum_memory.api.deps import get_db, get_current_user, check_namespace_read_access, check_board_permission
from forum_memory.api.rate_limit import limiter
from forum_memory.models.user import User
from forum_memory.schemas.memory import (
    MemoryCreate, MemoryUpdate, MemoryRead, MemoryFilter,
    AuthorityChange, MemorySearchRequest, MemorySearchResponse,
    MemoryBatchRequest,
)
from forum_memory.services import memory_service, search_service, extraction_service

router = APIRouter(prefix="/memories", tags=["memories"])


@router.get("", response_model=list[MemoryRead])
def list_memories(
    response: Response,
    filters: MemoryFilter = Depends(),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    items = memory_service.list_memories(session, filters, page, size)
    total = memory_service.count_memories(session, filters)
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/tags", response_model=list[str])
def list_tags(
    namespace_id: UUID | None = None,
    min_count: int = Query(2, ge=1),
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # 给定 namespace 时校验读权限；未给定时聚合所有板块的 tag
    # 仅对登录用户开放，非 PRIVATE 板块的标签是公开元数据
    if namespace_id is not None:
        check_namespace_read_access(namespace_id, session, user)
    return memory_service.list_all_tags(session, namespace_id, min_count=min_count)


@router.post("/batch", response_model=list[MemoryRead])
def batch_get(
    data: MemoryBatchRequest,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """批量取记忆。按 namespace 聚合一次性校验，过滤掉无权限的条目。"""
    memories = memory_service.batch_get_memories(session, data.ids)
    return _filter_readable(memories, session, user)


@router.get("/{memory_id}", response_model=MemoryRead)
def get_memory(
    memory_id: UUID,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    memory = memory_service.get_memory(session, memory_id)
    if not memory:
        raise HTTPException(404, "Memory not found")
    check_namespace_read_access(memory.namespace_id, session, user)
    return memory


def _filter_readable(memories, session: Session, user: User):
    """仅返回当前用户可读 namespace 的记忆。按 namespace 去重校验，避免 N 次权限查询。"""
    from forum_memory.api.deps import _is_namespace_member  # 复用成员判定
    from forum_memory.models.namespace import Namespace
    from forum_memory.models.enums import AccessMode, SystemRole

    if user.role == SystemRole.SUPER_ADMIN:
        return memories
    ns_ids = {m.namespace_id for m in memories}
    if not ns_ids:
        return memories
    ns_rows = list(session.exec(
        select(Namespace.id, Namespace.access_mode, Namespace.owner_id)
        .where(Namespace.id.in_(ns_ids))
    ).all())
    readable: set[UUID] = set()
    for nid, mode, owner in ns_rows:
        if mode != AccessMode.PRIVATE or owner == user.id or _is_namespace_member(nid, session, user):
            readable.add(nid)
    return [m for m in memories if m.namespace_id in readable]


@router.post("", response_model=MemoryRead, status_code=201)
def create_memory(data: MemoryCreate, session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    check_board_permission(data.namespace_id, session, user)
    return memory_service.create_memory(session, data)


@router.put("/{memory_id}", response_model=MemoryRead)
def update_memory(memory_id: UUID, data: MemoryUpdate, session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    memory = memory_service.get_memory(session, memory_id)
    if not memory:
        raise HTTPException(404, "Memory not found")
    check_board_permission(memory.namespace_id, session, user)
    updated = memory_service.update_memory(session, memory_id, data)
    if not updated:
        raise HTTPException(404, "Memory not found")
    return updated


@router.delete("/{memory_id}", status_code=204)
def delete_memory(memory_id: UUID, session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    memory = memory_service.get_memory(session, memory_id)
    if not memory:
        raise HTTPException(404, "Memory not found")
    check_board_permission(memory.namespace_id, session, user)
    ok = memory_service.delete_memory(session, memory_id)
    if not ok:
        raise HTTPException(404, "Memory not found")


@router.put("/{memory_id}/restore", response_model=MemoryRead)
def restore_memory(memory_id: UUID, session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Restore a COLD or ARCHIVED memory to ACTIVE status with immediate ES re-indexing."""
    memory = memory_service.get_memory(session, memory_id)
    if not memory:
        raise HTTPException(404, "Memory not found")
    check_board_permission(memory.namespace_id, session, user)
    restored = memory_service.restore_memory(session, memory_id)
    if not restored:
        raise HTTPException(404, "Memory not found")
    return restored


@router.put("/{memory_id}/authority", response_model=MemoryRead)
def change_authority(memory_id: UUID, data: AuthorityChange, session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    memory = memory_service.get_memory(session, memory_id)
    if not memory:
        raise HTTPException(404, "Memory not found")
    check_board_permission(memory.namespace_id, session, user)
    updated = memory_service.change_authority(session, memory_id, data.authority, data.reason)
    if not updated:
        raise HTTPException(404, "Memory not found")
    return updated


@router.post("/search", response_model=MemorySearchResponse)
@limiter.limit("20/minute")
def search(request: Request, data: MemorySearchRequest, session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    check_namespace_read_access(data.namespace_id, session, user)
    return search_service.search_memories(session, data)


@router.post("/extract/{thread_id}")
@limiter.limit("5/minute")
def extract(request: Request, thread_id: UUID, session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from forum_memory.models.thread import Thread
    thread = session.get(Thread, thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")
    check_board_permission(thread.namespace_id, session, user)
    try:
        ids = extraction_service.run_extraction(session, "thread", thread_id)
        return {"memory_ids_created": [str(i) for i in ids]}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
