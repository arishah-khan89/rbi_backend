"""Audit trail: one line per important action. Only ever added to the current database session (the caller commits)."""
from typing import Optional

from sqlalchemy.orm import Session

from app.models.audit import AuditLog


def record(db: Session, actor: str, action: str, target: Optional[str] = None, detail: Optional[str] = None) -> None:
    db.add(AuditLog(actor=actor, action=action, target=target, detail=detail))
