from pydantic import BaseModel, Field, field_validator
from app.core import unit_policy
from typing import Optional, List
from datetime import datetime
from decimal import Decimal
import uuid


class DynamicTMLReadingInput(BaseModel):
    tml_id: Optional[uuid.UUID] = None
    tml_code: str
    tml_name: Optional[str] = None
    tml_type: Optional[str] = "Shell"
    direction_labels: Optional[str] = "N,S,E,W"
    reading_1: Optional[Decimal] = None
    reading_2: Optional[Decimal] = None
    reading_3: Optional[Decimal] = None
    reading_4: Optional[Decimal] = None
    reading_5: Optional[Decimal] = None
    reading_6: Optional[Decimal] = None
    min_required_thickness: Optional[Decimal] = None


class InspectionRecordBase(BaseModel):
    equipment_tag: str
    inspection_date: datetime
    inspector_name: Optional[str] = "Lead Inspector"
    inspection_type: Optional[str] = "Ultrasonic Thickness Measurement (UTM)"
    thickness_unit: Optional[str] = "mm"

    @field_validator("thickness_unit")
    @classmethod
    def _readings_in_mm(cls, v):
        return unit_policy.length_unit(v, "Thickness readings")

    # Shell 1 Readings
    s1_n: Optional[Decimal] = None
    s1_s: Optional[Decimal] = None
    s1_e: Optional[Decimal] = None
    s1_w: Optional[Decimal] = None

    # Shell 2 Readings
    s2_n: Optional[Decimal] = None
    s2_s: Optional[Decimal] = None
    s2_e: Optional[Decimal] = None
    s2_w: Optional[Decimal] = None

    # Dish 1 Readings
    d1_n: Optional[Decimal] = None
    d1_s: Optional[Decimal] = None
    d1_e: Optional[Decimal] = None
    d1_w: Optional[Decimal] = None

    # Dish 2 Readings
    d2_n: Optional[Decimal] = None
    d2_s: Optional[Decimal] = None
    d2_e: Optional[Decimal] = None
    d2_w: Optional[Decimal] = None

    # Dynamic TML readings list for custom zones
    tml_readings: Optional[List[DynamicTMLReadingInput]] = None

    remarks: Optional[str] = None

    model_config = {"from_attributes": True}


class InspectionRecordCreate(InspectionRecordBase):
    equipment_id: uuid.UUID
    created_by: Optional[str] = "System"


class InspectionRecordResponse(InspectionRecordBase):
    id: uuid.UUID
    equipment_id: uuid.UUID
    min_measured_thickness: Optional[Decimal] = None
    corrosion_rate: Optional[Decimal] = None
    remaining_life: Optional[Decimal] = None
    next_inspection_due: Optional[datetime] = None

    # Component-wise remaining life
    shell_remaining_life: Optional[Decimal] = None
    head_remaining_life: Optional[Decimal] = None
    tube_remaining_life: Optional[Decimal] = None
    roof_remaining_life: Optional[Decimal] = None
    bottom_remaining_life: Optional[Decimal] = None

    # API 510 dual inspection due dates
    next_internal_due: Optional[datetime] = None
    next_external_due: Optional[datetime] = None

    # Approval workflow
    approval_status: str = "draft"
    submitted_by: Optional[str] = None
    submitted_at: Optional[datetime] = None
    assigned_reviewer: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    submit_comment: Optional[str] = None
    review_comment: Optional[str] = None
    # Set when the t_min of a zone has changed since this (locked) inspection was sent for approval
    t_min_note: Optional[str] = None

    created_at: Optional[datetime] = None
    created_by: Optional[str] = None
    updated_at: Optional[datetime] = None


class SubmitRequest(BaseModel):
    reviewer: str                       # username of the ONE admin who will review it
    comment: Optional[str] = None


class DecisionRequest(BaseModel):
    comment: Optional[str] = None


class ReturnRequest(BaseModel):
    comment: str = Field(min_length=3, max_length=1000)     # what must be corrected


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class ApprovalSummary(BaseModel):
    assigned_to_me: int = 0             # waiting for MY decision
    returned_to_me: int = 0             # I sent them, the admin returned them
    pending_total: int = 0


class TaskItem(BaseModel):
    """One line of "My tasks": light on purpose (no readings), enough to show it and to open it."""
    id: uuid.UUID
    equipment_id: uuid.UUID
    equipment_tag: str
    inspection_date: datetime
    approval_status: str
    submitted_by: Optional[str] = None
    submitted_at: Optional[datetime] = None
    assigned_reviewer: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    review_comment: Optional[str] = None

    model_config = {"from_attributes": True}


class MyTasks(BaseModel):
    """What the signed-in person has to do or is waiting for. Each list is capped; the *_total says how many there are."""
    awaiting_my_decision: list[TaskItem] = []      # an admin: sent to me, I have to approve or return
    returned: list[TaskItem] = []                  # I sent them, an admin returned them: correct and send again
    drafts: list[TaskItem] = []                    # mine, not sent yet
    waiting_for_approval: list[TaskItem] = []      # I sent them, still with the reviewer
    recently_approved: list[TaskItem] = []         # mine, approved lately
    awaiting_my_decision_total: int = 0
    returned_total: int = 0
    drafts_total: int = 0
    waiting_for_approval_total: int = 0
    pending_in_system: int = 0                     # everybody's, for the super admin's overview


class DecidedItem(BaseModel):
    """One decision an admin took (Approved or Returned), with the inspection it was about."""
    audit_id: uuid.UUID
    decided_at: datetime
    decision: str                           # "approved" | "returned"
    decided_by: str
    comment: Optional[str] = None
    record_id: Optional[uuid.UUID] = None   # empty when the inspection no longer exists
    equipment_tag: Optional[str] = None
    inspection_date: Optional[datetime] = None
    inspector_name: Optional[str] = None
    submitted_by: Optional[str] = None
    reviewed_by: Optional[str] = None       # who holds the inspection's latest decision (the Reopen button depends on it)
    current_status: Optional[str] = None    # where the inspection stands now (e.g. approved, later reopened -> returned)


class InspectionRecordListResponse(BaseModel):
    data: list[InspectionRecordResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class InspectionStats(BaseModel):
    total_inspections: int
    critical_life_count: int
    warning_life_count: int
    healthy_life_count: int
