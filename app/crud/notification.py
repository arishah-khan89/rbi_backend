"""Notifications: one list per person, newest first, with read / unread.

`notify` only adds the row; the caller commits together with the change that caused it, so a notification never
exists for something that was rolled back.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.notification import Notification

RETENTION_DAYS = 30          # older notifications are removed
LIST_LIMIT = 50


def notify(db: Session, recipient: Optional[str], kind: str, message: str, detail: Optional[str] = None,
           record=None, actor: Optional[str] = None) -> None:
    """Tell `recipient` something happened. Nobody is notified of what they did themselves."""
    if not recipient or recipient.strip().lower() in ("system", (actor or "").strip().lower()):
        return
    db.add(Notification(
        recipient=recipient.strip().lower(), kind=kind, message=message, detail=(detail or None),
        record_id=getattr(record, "id", None), equipment_id=getattr(record, "equipment_id", None),
    ))


def _purge(db: Session, username: str) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    removed = db.query(Notification).filter(Notification.recipient == username, Notification.created_at < cutoff) \
        .delete(synchronize_session=False)
    if removed:
        db.commit()


def list_for(db: Session, username: str, unread_only: bool = False, limit: int = LIST_LIMIT) -> dict:
    me = username.lower()
    _purge(db, me)
    q = db.query(Notification).filter(Notification.recipient == me)
    unread = q.filter(Notification.read_at.is_(None)).count()
    if unread_only:
        q = q.filter(Notification.read_at.is_(None))
    return {"unread": unread, "items": q.order_by(Notification.created_at.desc()).limit(limit).all()}


def mark_read(db: Session, username: str, notification_id) -> Optional[Notification]:
    """Only the recipient can mark their own notification; anything else is simply 'not found'."""
    n = db.query(Notification).filter(Notification.id == notification_id, Notification.recipient == username.lower()).first()
    if n is not None and n.read_at is None:
        n.read_at = datetime.now(timezone.utc)
        db.commit()
    return n


def mark_all_read(db: Session, username: str) -> int:
    count = db.query(Notification).filter(Notification.recipient == username.lower(), Notification.read_at.is_(None)) \
        .update({Notification.read_at: datetime.now(timezone.utc)}, synchronize_session=False)
    db.commit()
    return count
