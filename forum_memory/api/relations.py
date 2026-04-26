"""Memory relation API routes."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from forum_memory.api.deps import get_db, get_current_user, check_board_permission
from forum_memory.models.user import User
from forum_memory.models.enums import RelationType
from forum_memory.schemas.relation import RelationCreate, RelationRead
from forum_memory.services import relation_service, memory_service

router = APIRouter(prefix="/memories", tags=["relations"])


@router.get("/{memory_id}/relations", response_model=list[RelationRead])
def list_relations(
    memory_id: UUID,
    session: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """List all relations where this memory is source or target."""
    memory = memory_service.get_memory(session, memory_id)
    if not memory:
        raise HTTPException(404, "Memory not found")
    return relation_service.list_relations(session, memory_id)


@router.post("/{memory_id}/relations", response_model=RelationRead, status_code=201)
def create_relation(
    memory_id: UUID,
    data: RelationCreate,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Manually create a relation between two memories."""
    memory = memory_service.get_memory(session, memory_id)
    if not memory:
        raise HTTPException(404, "Source memory not found")
    check_board_permission(memory.namespace_id, session, user)
    try:
        rel_type = RelationType(data.relation_type)
    except ValueError as e:
        raise HTTPException(400, f"Invalid relation_type: {data.relation_type}") from e
    rel = relation_service.create_relation(
        session, memory_id, data.target_memory_id,
        rel_type, data.confidence, origin="manual",
    )
    if not rel:
        raise HTTPException(400, "Cannot create relation (memories not found or self-loop)")
    return rel


@router.delete("/relations/{relation_id}", status_code=204)
def delete_relation(
    relation_id: UUID,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete a relation (requires board permission on source memory's namespace)."""
    from forum_memory.models.memory_relation import MemoryRelation

    rel = session.get(MemoryRelation, relation_id)
    if not rel:
        raise HTTPException(404, "Relation not found")
    # 优先用 source 的 namespace 校验；source 已硬删时退化用 target；都缺失则拒绝。
    source_mem = memory_service.get_memory(session, rel.source_memory_id)
    target_mem = memory_service.get_memory(session, rel.target_memory_id)
    if source_mem:
        check_board_permission(source_mem.namespace_id, session, user)
    elif target_mem:
        check_board_permission(target_mem.namespace_id, session, user)
    else:
        raise HTTPException(403, "无法验证关系归属，关联可能已孤立")
    if not relation_service.delete_relation(session, relation_id):
        raise HTTPException(404, "Relation not found")
