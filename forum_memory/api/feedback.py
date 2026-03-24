"""Feedback API routes — sync."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from forum_memory.api.deps import get_db, get_current_user_id
from forum_memory.schemas.feedback import FeedbackCreate, FeedbackRead, FeedbackSummary, FeedbackWithdraw
from forum_memory.services import feedback_service

router = APIRouter(tags=["feedback"])


@router.post("/memories/{memory_id}/feedback", response_model=FeedbackRead, status_code=201)
def submit_feedback(memory_id: UUID, data: FeedbackCreate, session: Session = Depends(get_db), user_id: UUID = Depends(get_current_user_id)):
    return feedback_service.submit_feedback(session, memory_id, data, user_id)


@router.delete("/memories/{memory_id}/feedback")
def withdraw_feedback(memory_id: UUID, data: FeedbackWithdraw, session: Session = Depends(get_db), user_id: UUID = Depends(get_current_user_id)):
    ok = feedback_service.withdraw_feedback(session, memory_id, data.feedback_type, user_id)
    if not ok:
        raise HTTPException(404, "Feedback not found")
    return {"ok": True}


@router.get("/memories/{memory_id}/feedback", response_model=list[FeedbackRead])
def list_feedback(memory_id: UUID, session: Session = Depends(get_db)):
    return feedback_service.list_feedback(session, memory_id)


@router.get("/memories/{memory_id}/feedback/mine")
def my_feedback(
    memory_id: UUID,
    session: Session = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
):
    """Return the current user's feedback type on a memory."""
    fb_type = feedback_service.get_my_feedback(session, memory_id, user_id)
    return {"feedback_type": fb_type}


@router.get("/memories/{memory_id}/feedback/summary", response_model=FeedbackSummary)
def feedback_summary(memory_id: UUID, session: Session = Depends(get_db)):
    return feedback_service.get_summary(session, memory_id)
