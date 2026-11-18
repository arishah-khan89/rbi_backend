import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class NotificationOut(BaseModel):
    id: uuid.UUID
    kind: str
    message: str
    detail: Optional[str] = None
    record_id: Optional[uuid.UUID] = None
    equipment_id: Optional[uuid.UUID] = None
    created_at: datetime
    read_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class NotificationList(BaseModel):
    unread: int = 0
    items: list[NotificationOut] = []
