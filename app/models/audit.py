import uuid

from sqlalchemy import Column, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class AuditLog(Base):
    """Who did what and when (user administration now, inspection approvals next). Rows are only ever added."""
    __tablename__ = "audit_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    actor = Column(String(80), nullable=False)            # username of the person who did it
    action = Column(String(60), nullable=False, index=True)   # e.g. user.create, user.role, user.deactivate
    target = Column(String(200))                          # what it was done to (a username, later a record)
    detail = Column(Text)                                 # e.g. "role: engineer -> admin"
