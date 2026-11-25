import uuid

from sqlalchemy import Column, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base

TASK_TYPES = ("Internal", "External", "UTG", "NDE")


class InspectionTask(Base):
    """A piece of inspection work planned for an equipment (Internal, External, UTG, NDE) with when it was last done and
    when it is due next. A task written by a person ('manual') holds what they wrote. A task made from an approved risk
    assessment ('rbi') takes its RBI interval and dates from that assessment's inspection plan."""
    __tablename__ = "inspection_tasks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    equipment_id = Column(UUID(as_uuid=True), ForeignKey("equipment.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(200), nullable=False)                    # e.g. "Inspection Task-1"
    task_type = Column(String(20), nullable=False)                # Internal | External | UTG | NDE
    details = Column(Text)
    duration_days = Column(Integer)                               # how long the work takes, in days
    rbi_interval_years = Column(Numeric(6, 2))                    # the RBI-based interval, in years (typed by the person)
    last_date = Column(Date)
    next_date = Column(Date, index=True)
    source = Column(String(10), nullable=False, default="manual", server_default="manual")     # manual | rbi
    rbi_component_id = Column(UUID(as_uuid=True), ForeignKey("rbi_components.id", ondelete="SET NULL"), index=True)
    rbi_assessment_id = Column(UUID(as_uuid=True), ForeignKey("rbi_assessments.id", ondelete="SET NULL"))

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    created_by = Column(String(200))
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    updated_by = Column(String(200))
