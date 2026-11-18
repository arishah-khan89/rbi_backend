import uuid

from sqlalchemy import Column, Date, DateTime, ForeignKey, Integer, LargeBinary, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import deferred

from app.core.database import Base

REPORT_TITLES = ("Baseline", "Routine", "Leak", "Repair", "Other")


class InspectionReport(Base):
    """An inspection report of one equipment: what was inspected and why, a summary, the tasks it covers and its files.
    It goes through the same approval as an inspection: draft -> pending (sent to ONE admin) -> approved (locked) | returned."""
    __tablename__ = "inspection_reports"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    equipment_id = Column(UUID(as_uuid=True), ForeignKey("equipment.id", ondelete="CASCADE"), nullable=False, index=True)
    equipment_tag = Column(String(50), nullable=False, index=True)
    report_no = Column(String(50), nullable=False)                 # written by the person; unique (see crud)
    inspection_date = Column(Date, nullable=False, index=True)
    title = Column(String(40), nullable=False)                     # Baseline | Routine | Leak | Repair | Other
    reason = Column(Text)
    summary = Column(Text)

    approval_status = Column(String(20), nullable=False, default="draft", server_default="draft", index=True)
    submitted_by = Column(String(80))
    submitted_at = Column(DateTime(timezone=True))
    assigned_reviewer = Column(String(80))                         # the only person who may approve / return it
    reviewed_by = Column(String(80))
    reviewed_at = Column(DateTime(timezone=True))
    submit_comment = Column(Text)
    review_comment = Column(Text)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    created_by = Column(String(200))
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    updated_by = Column(String(200))


class InspectionReportTask(Base):
    """Which inspection tasks a report covers (one or more)."""
    __tablename__ = "inspection_report_tasks"

    report_id = Column(UUID(as_uuid=True), ForeignKey("inspection_reports.id", ondelete="CASCADE"), primary_key=True)
    task_id = Column(UUID(as_uuid=True), ForeignKey("inspection_tasks.id", ondelete="RESTRICT"), primary_key=True, index=True)


class InspectionReportRecipient(Base):
    """"Send to": the people who are told about a report (the approver is chosen separately when it is sent)."""
    __tablename__ = "inspection_report_recipients"

    report_id = Column(UUID(as_uuid=True), ForeignKey("inspection_reports.id", ondelete="CASCADE"), primary_key=True)
    username = Column(String(80), primary_key=True)                # lower case


class InspectionReportFile(Base):
    """A file attached to a report. The bytes live in the database (so they are backed up with it and survive a redeploy)
    and are only ever given to a signed-in person through the API, never from a public folder."""
    __tablename__ = "inspection_report_files"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    report_id = Column(UUID(as_uuid=True), ForeignKey("inspection_reports.id", ondelete="CASCADE"), nullable=False, index=True)
    filename = Column(String(200), nullable=False)                 # safe version of the name it was uploaded with
    content_type = Column(String(100))
    size_bytes = Column(Integer, nullable=False)
    content = deferred(Column(LargeBinary, nullable=False))        # not loaded with the list of files
    uploaded_by = Column(String(200))
    uploaded_at = Column(DateTime(timezone=True), server_default=func.now())
