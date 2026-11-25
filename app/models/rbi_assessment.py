"""Risk assessment (RBI) of the client's workbook "030-E-11- Shell.xlsx": the reference lists, the components of an equipment and
their analyses. The scores and the risk are worked out by app/rbi_engine from the person's inputs and the workbook's rules; an analysis
keeps its inputs, and (once finalized) a copy of the result and the version of the rules it was worked out with."""
import uuid

from sqlalchemy import Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.database import Base

STATES = ("draft", "finalized", "approved", "superseded")


class RbiFluidEntry(Base):
    """One row of the workbook's "list of fluids": a fluid can be a toxic fluid (column A), a flammable / representative fluid
    (column B) or both. The toxicity concentration is the number the toxicity score is looked up with; flammability is the 0-4
    rating (score = rating + 1); the flash point is a number in degrees C or the sheet's own words ("N/A", "Data missing")."""
    __tablename__ = "rbi_fluid_list"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    toxic_name = Column(String(200))
    flammable_name = Column(String(200))
    cas_number = Column(String(80))
    toxicity_concentration = Column(Numeric(14, 4))
    toxicity_unit = Column(String(30))
    flammability = Column(Integer)
    flash_point = Column(String(40))
    note = Column(String(200))                                   # the sheet's column C (e.g. "Meridium toxic")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


Index("uq_rbi_fluid_list_toxic_lower", func.lower(RbiFluidEntry.toxic_name), unique=True,
      postgresql_where=RbiFluidEntry.toxic_name.isnot(None))
Index("uq_rbi_fluid_list_flammable_lower", func.lower(RbiFluidEntry.flammable_name), unique=True,
      postgresql_where=RbiFluidEntry.flammable_name.isnot(None))


class RbiMechanismEntry(Base):
    """A damage mechanism of the workbook's NDE_Library (location, class, rate-based or not, typical NDE, mitigation), with the
    extra text of its "damages list" row when the same mechanism is there (definition, materials, temperatures, streams)."""
    __tablename__ = "rbi_mechanism_library"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code = Column(String(20), nullable=False, index=True)        # DM01 ... (the client's ID; not unique: the sheet repeats one)
    name = Column(String(200), nullable=False)
    location = Column(String(20))                                # Internal | External (empty when the sheet gives none)
    damage_class = Column(String(60))
    rate_based = Column(String(10))                              # Yes | No
    typical_drivers = Column(Text)
    primary_nde = Column(Text)
    secondary_nde = Column(Text)
    coverage_timing = Column(Text)
    mitigation = Column(Text)
    definition = Column(Text)
    applicable_materials = Column(Text)
    min_temperature_c = Column(Numeric(8, 2))
    max_temperature_c = Column(Numeric(8, 2))
    temperature_note = Column(String(200))
    process_streams = Column(Text)
    damage_type = Column(String(200))
    recommended_nde = Column(Text)
    sort_order = Column(Integer)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


Index("uq_rbi_mechanism_library_name_lower", func.lower(RbiMechanismEntry.name), unique=True)


class RbiComponent(Base):
    """A part of an equipment that is assessed on its own (Shell, Head / Dish, Tube bundle, Roof, Bottom ...)."""
    __tablename__ = "rbi_components"
    __table_args__ = (UniqueConstraint("equipment_id", "name", name="uq_rbi_component_name"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    equipment_id = Column(UUID(as_uuid=True), ForeignKey("equipment.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    component_type = Column(String(40), nullable=False)
    in_service_date = Column(Date)
    status = Column(String(20), nullable=False, default="Active")
    description = Column(Text)
    comments = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    created_by = Column(String(200))
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    updated_by = Column(String(200))


class RbiAssessment(Base):
    """One analysis (revision) of a component. draft -> finalized (risk completed, waits for an admin) -> approved (locked).
    A newer revision supersedes the older one when it is approved."""
    __tablename__ = "rbi_assessments"
    __table_args__ = (UniqueConstraint("component_id", "revision", name="uq_rbi_assessment_revision"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    component_id = Column(UUID(as_uuid=True), ForeignKey("rbi_components.id", ondelete="CASCADE"), nullable=False, index=True)
    revision = Column(Integer, nullable=False)
    state = Column(String(20), nullable=False, default="draft", index=True)
    assessment_date = Column(Date, nullable=False)
    inputs = Column(JSONB, nullable=False, default=dict)
    results = Column(JSONB)                                      # the worked-out result, kept when the analysis is finalized
    rules_version = Column(String(30))                           # the version of the rules that result was worked out with
    comments = Column(Text)
    return_reason = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    created_by = Column(String(200))
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    updated_by = Column(String(200))
    finalized_at = Column(DateTime(timezone=True))
    finalized_by = Column(String(200))
    approved_at = Column(DateTime(timezone=True))
    approved_by = Column(String(200))
    assigned_reviewer = Column(String(80))                       # the admin it was sent to while it is finalized: only that admin decides
    submit_comment = Column(Text)                                # what the person wrote when sending it
    task_plan = Column(JSONB)                                    # per side (internal / external): new task, link an existing task, or none; filled in by the approval
