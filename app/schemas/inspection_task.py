import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.models.inspection_task import TASK_TYPES


def _clean(v: Optional[str]) -> Optional[str]:
    return v.strip() if isinstance(v, str) else v


class TaskFields(BaseModel):
    """What a person writes about a task. Every value is theirs; the software calculates nothing here."""
    name: Optional[str] = Field(default=None, max_length=200)
    task_type: Optional[str] = None
    details: Optional[str] = Field(default=None, max_length=4000)
    duration_days: Optional[int] = Field(default=None, ge=0, le=3650)              # how long the work takes, in days
    rbi_interval_years: Optional[Decimal] = Field(default=None, gt=0, le=50, decimal_places=2)
    last_date: Optional[date] = None
    next_date: Optional[date] = None

    @field_validator("name", "details", mode="before")
    @classmethod
    def _trim(cls, v):
        v = _clean(v)
        return v or None

    @field_validator("task_type")
    @classmethod
    def _known_type(cls, v):
        if v is not None and v not in TASK_TYPES:
            raise ValueError(f"Type of task must be one of: {', '.join(TASK_TYPES)}.")
        return v


class TaskCreate(TaskFields):
    equipment_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    task_type: str

    @field_validator("name", mode="before")
    @classmethod
    def _name_needed(cls, v):
        v = _clean(v)
        if not v:
            raise ValueError("Give the task a name.")
        return v


class TaskUpdate(TaskFields):
    """Only the fields that are sent are changed (the equipment of a task cannot be changed)."""


class TaskResponse(BaseModel):
    id: uuid.UUID
    equipment_id: uuid.UUID
    equipment_tag: Optional[str] = None
    equipment_name: Optional[str] = None
    name: str
    task_type: str
    details: Optional[str] = None
    duration_days: Optional[int] = None
    rbi_interval_years: Optional[Decimal] = None
    last_date: Optional[date] = None
    next_date: Optional[date] = None
    status: str = "none"                  # overdue | due_soon | ok | none (no next date)
    created_at: Optional[datetime] = None
    created_by: Optional[str] = None
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None

    model_config = {"from_attributes": True}


class TaskListResponse(BaseModel):
    data: list[TaskResponse]
    total: int
    page: int
    page_size: int
    total_pages: int
    counts: dict[str, int] = {}           # how many are overdue / due_soon / ok / none in the whole filtered list
