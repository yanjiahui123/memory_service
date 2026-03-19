"""Notification API routes."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlmodel import Session

from forum_memory.api.deps import get_db, get_current_user
from forum_memory.models.user import User
from forum_memory.schemas.notification import NotificationRead
from forum_memory.services import notification_service

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/unread-count")
def get_unread_count(
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Return unread notification count. Polled by frontend every 30s."""
    count = notification_service.get_unread_count(session, user.id)
    return {"unread_count": count}


@router.get("", response_model=list[NotificationRead])
def list_notifications(
    response: Response,
    unread_only: bool = False,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=50),
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List notifications for current user with pagination."""
    items, total = notification_service.list_notifications(
        session, user.id, page, size, unread_only,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.post("/{notification_id}/read", status_code=204)
def mark_read(
    notification_id: UUID,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Mark a single notification as read."""
    notification_service.mark_as_read(session, notification_id, user.id)


@router.post("/read-all", status_code=204)
def mark_all_read(
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Mark all notifications as read for current user."""
    notification_service.mark_all_as_read(session, user.id)
