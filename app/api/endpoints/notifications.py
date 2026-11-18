import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import Principal, authorize
from app.core.config import settings
from app.core.database import get_db
from app.crud import notification as crud
from app.schemas.notification import NotificationList, NotificationOut

router = APIRouter()


@router.get("", response_model=NotificationList)
def my_notifications(unread_only: bool = False, limit: int = Query(30, ge=1, le=crud.LIST_LIMIT),
                     db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    """The signed-in person's notifications, newest first, with how many are unread."""
    if not settings.AUTH_REQUIRED:
        return NotificationList()
    return NotificationList(**crud.list_for(db, principal.username, unread_only, limit))


@router.post("/read-all")
def read_all(db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    if not settings.AUTH_REQUIRED:
        return {"marked": 0}
    return {"marked": crud.mark_all_read(db, principal.username)}


@router.post("/{notification_id}/read", response_model=NotificationOut)
def read_one(notification_id: uuid.UUID, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    n = crud.mark_read(db, principal.username, notification_id) if settings.AUTH_REQUIRED else None
    if n is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Notification not found.")
    return n
