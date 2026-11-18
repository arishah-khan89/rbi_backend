import uuid
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.models.inspection_report import REPORT_TITLES


def _clean(v):
    return v.strip() if isinstance(v, str) else v


class ReportFields(BaseModel):
    """What a person writes in a report. Fields that are left out of an update are not changed."""
    report_no: Optional[str] = Field(default=None, max_length=50)
    inspection_date: Optional[date] = None
    title: Optional[str] = None
    reason: Optional[str] = Field(default=None, max_length=4000)
    summary: Optional[str] = Field(default=None, max_length=8000)
    task_ids: Optional[list[uuid.UUID]] = None            # the tasks the report covers (one or more)
    recipients: Optional[list[str]] = None                # "Send to": usernames

    @field_validator("report_no", "reason", "summary", mode="before")
    @classmethod
    def _trim(cls, v):
        v = _clean(v)
        return v or None

    @field_validator("title")
    @classmethod
    def _known_title(cls, v):
        if v is not None and v not in REPORT_TITLES:
            raise ValueError(f"Title must be one of: {', '.join(REPORT_TITLES)}.")
        return v


class ReportCreate(ReportFields):
    equipment_id: uuid.UUID
    report_no: str = Field(min_length=1, max_length=50)
    inspection_date: date
    title: str

    @field_validator("report_no", mode="before")
    @classmethod
    def _no_needed(cls, v):
        v = _clean(v)
        if not v:
            raise ValueError("Write the report number.")
        return v


class ReportUpdate(ReportFields):
    """The equipment of a report cannot be changed."""


class ReportTaskOut(BaseModel):
    id: uuid.UUID
    name: str
    task_type: str
    last_date: Optional[date] = None
    next_date: Optional[date] = None

    model_config = {"from_attributes": True}


class ReportFileOut(BaseModel):
    id: uuid.UUID
    filename: str
    content_type: Optional[str] = None
    size_bytes: int
    uploaded_by: Optional[str] = None
    uploaded_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class RecipientOut(BaseModel):
    username: str
    full_name: Optional[str] = None
    role: Optional[str] = None


class ReportListItem(BaseModel):
    id: uuid.UUID
    equipment_id: uuid.UUID
    equipment_tag: str
    equipment_name: Optional[str] = None
    report_no: str
    inspection_date: date
    title: str
    approval_status: str
    submitted_by: Optional[str] = None
    assigned_reviewer: Optional[str] = None
    reviewed_by: Optional[str] = None
    task_count: int = 0
    file_count: int = 0
    created_by: Optional[str] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ReportOut(ReportListItem):
    reason: Optional[str] = None
    summary: Optional[str] = None
    submitted_at: Optional[datetime] = None
    reviewed_at: Optional[datetime] = None
    submit_comment: Optional[str] = None
    review_comment: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_by: Optional[str] = None
    tasks: list[ReportTaskOut] = []
    files: list[ReportFileOut] = []
    recipients: list[RecipientOut] = []


class ReportListResponse(BaseModel):
    data: list[ReportListItem]
    total: int
    page: int
    page_size: int
    total_pages: int
    counts: dict[str, int] = {}           # per state, for the whole filtered list (ignoring the state filter itself)


class ReportSummary(BaseModel):
    waiting_for_me: int = 0               # an admin: sent to me, I have to approve or return
    returned_to_me: int = 0               # I sent them, an admin returned them
    my_drafts: int = 0


class ReportSubmit(BaseModel):
    approver: str                         # the ONE admin who will approve or return it
    comment: Optional[str] = None


class ReportDecision(BaseModel):
    comment: Optional[str] = None


class ReportReturn(BaseModel):
    comment: str = Field(min_length=3, max_length=1000)


class ReportReason(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
