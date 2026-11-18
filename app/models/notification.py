import uuid

from sqlalchemy import Column, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class Notification(Base):
    """A message for one person: an inspection was sent to them, returned, approved, reopened or withdrawn.
    It stays in their list until it is older than the retention period; `read_at` is empty until they open it."""
    __tablename__ = "notifications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recipient = Column(String(80), nullable=False, index=True)      # username (lower case)
    kind = Column(String(30), nullable=False)                       # assigned | returned | approved | reopened | withdrawn
    message = Column(Text, nullable=False)
    detail = Column(Text)                                           # the comment or reason that came with it
    record_id = Column(UUID(as_uuid=True))                          # the inspection it is about (the link goes there)
    equipment_id = Column(UUID(as_uuid=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    read_at = Column(DateTime(timezone=True))
