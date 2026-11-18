from datetime import datetime
from decimal import Decimal
from sqlalchemy.orm import Session
from sqlalchemy import or_, func
from typing import Optional
from app.core import audit
from app.models.equipment import Equipment
from app.schemas.equipment import EquipmentCreate, EquipmentUpdate, EquipmentStats


def get_equipment_list(
    db: Session,
    search: Optional[str] = None,
    equipment_type: Optional[str] = None,
    plant: Optional[str] = None,
    unit: Optional[str] = None,
    status: Optional[str] = None,
    criticality: Optional[str] = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
    page: int = 1,
    page_size: int = 25,
):
    query = db.query(Equipment).filter(Equipment.status != "DELETED")

    # Search
    if search:
        search_term = f"%{search}%"
        query = query.filter(
            or_(
                Equipment.equipment_id.ilike(search_term),
                Equipment.equipment_name.ilike(search_term),
                Equipment.equipment_type.ilike(search_term),
                Equipment.plant.ilike(search_term),
                Equipment.unit.ilike(search_term),
                Equipment.area.ilike(search_term),
                Equipment.service.ilike(search_term),
                Equipment.asset_tag.ilike(search_term),
            )
        )

    # Filters
    if equipment_type:
        query = query.filter(Equipment.equipment_type == equipment_type)
    if plant:
        query = query.filter(Equipment.plant == plant)
    if unit:
        query = query.filter(Equipment.unit == unit)
    if status:
        query = query.filter(Equipment.status == status.upper())
    if criticality:
        query = query.filter(Equipment.criticality == criticality.upper())

    # Sorting
    sortable_columns = {
        "equipment_id": Equipment.equipment_id,
        "equipment_name": Equipment.equipment_name,
        "equipment_type": Equipment.equipment_type,
        "plant": Equipment.plant,
        "unit": Equipment.unit,
        "status": Equipment.status,
        "criticality": Equipment.criticality,
        "manufacturing_year": Equipment.manufacturing_year,
        "created_at": Equipment.created_at,
        "updated_at": Equipment.updated_at,
    }
    col = sortable_columns.get(sort_by, Equipment.created_at)
    if sort_order == "asc":
        query = query.order_by(col.asc())
    else:
        query = query.order_by(col.desc())

    total = query.count()
    items = query.offset((page - 1) * page_size).limit(page_size).all()
    total_pages = (total + page_size - 1) // page_size

    return items, total, total_pages


def get_inspections_due(db: Session, limit: int = 5):
    """Assets with a calculated next inspection date, the earliest (most overdue) first."""
    earliest = func.least(
        func.coalesce(Equipment.next_internal_inspection_date, Equipment.next_external_inspection_date),
        func.coalesce(Equipment.next_external_inspection_date, Equipment.next_internal_inspection_date),
    )
    return (
        db.query(Equipment)
        .filter(Equipment.status != "DELETED")
        .filter(or_(Equipment.next_internal_inspection_date.isnot(None),
                    Equipment.next_external_inspection_date.isnot(None)))
        .order_by(earliest.asc())
        .limit(limit)
        .all()
    )


def get_equipment_by_uuid(db: Session, uuid: str) -> Optional[Equipment]:
    return db.query(Equipment).filter(Equipment.id == uuid).first()


def get_equipment_by_equipment_id(db: Session, equipment_id: str) -> Optional[Equipment]:
    return db.query(Equipment).filter(Equipment.equipment_id == equipment_id).first()


# The minimum thicknesses (t_min): every calculated remaining life depends on them
T_MIN_FIELDS = ("min_required_thickness", "dish_min_thickness", "tube_min_thickness", "roof_min_thickness",
                "floating_roof_min_thickness", "bottom_min_thickness")
# Written by the inspection engine, not by a person: never listed as a change a person made
CALCULATED_FIELDS = {"remaining_life", "corrosion_rate", "shell_controlling_life", "head_controlling_life",
                     "tube_controlling_life", "roof_controlling_life", "bottom_controlling_life",
                     "next_internal_inspection_date", "next_external_inspection_date", "updated_by", "updated_at",
                     "created_by", "created_at"}


class ReasonRequiredError(ValueError):
    """A minimum thickness is being changed without saying why."""


def _same(old, new) -> bool:
    if old in (None, "") and new in (None, ""):
        return True
    if isinstance(old, (Decimal, int, float)) and isinstance(new, (Decimal, int, float)):
        return Decimal(str(old)) == Decimal(str(new))
    if isinstance(old, datetime) and isinstance(new, datetime):
        return old == new
    return (str(old).strip() if old is not None else None) == (str(new).strip() if new is not None else None)


def _show(value) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value)


def create_equipment(db: Session, data: EquipmentCreate, actor: str = "System") -> Equipment:
    equipment = Equipment(**data.model_dump())
    db.add(equipment)
    db.flush()
    audit.record(db, actor, "equipment.create", str(equipment.id),
                 f"{equipment.equipment_id} | {equipment.equipment_name} ({equipment.equipment_type})")
    db.commit()
    db.refresh(equipment)
    return equipment


def update_equipment(db: Session, uuid: str, data: EquipmentUpdate, actor: str = "System",
                     reason_required: bool = False) -> Optional[Equipment]:
    """Saves the changes, writes WHO changed WHAT (old -> new) to the audit trail, and recalculates the equipment.
    A change of a minimum thickness needs a written reason when sign-in is on (reason_required). Inspections that are
    locked keep the t_min they were locked with (see crud/inspection.py)."""
    equipment = get_equipment_by_uuid(db, uuid)
    if not equipment:
        return None

    update_data = data.model_dump(exclude_unset=True)
    reason = (update_data.pop("change_reason", None) or "").strip()
    changes = {f: (getattr(equipment, f, None), v) for f, v in update_data.items()
               if f not in CALCULATED_FIELDS and hasattr(Equipment, f) and not _same(getattr(equipment, f, None), v)}
    if reason_required and any(f in T_MIN_FIELDS for f in changes) and len(reason) < 3:
        raise ReasonRequiredError("Write the reason for changing the minimum thickness (t_min): it is kept in the audit log.")

    if changes:
        from app.crud.inspection import ensure_locked_snapshots
        ensure_locked_snapshots(db, equipment.id)          # locked inspections remember today's t_min before it changes

    for field, value in update_data.items():
        setattr(equipment, field, value)
    if changes:
        lines = "; ".join(f"{f}: {_show(old)} -> {_show(new)}" for f, (old, new) in changes.items())
        audit.record(db, actor, "equipment.edit", str(equipment.id),
                     f"{equipment.equipment_id} | {lines}" + (f" | reason: {reason}" if reason else ""))
    db.commit()
    db.refresh(equipment)
    
    # Always recalculate CMLs on equipment update to keep life calculations strictly synchronized with equipment specs
    from app.crud.cml import recalculate_equipment_cmls
    recalculate_equipment_cmls(db, equipment.id)
        
    return equipment


def soft_delete_equipment(db: Session, uuid: str, deleted_by: str = "System") -> Optional[Equipment]:
    equipment = get_equipment_by_uuid(db, uuid)
    if not equipment:
        return None
    equipment.status = "DELETED"
    equipment.updated_by = deleted_by
    audit.record(db, deleted_by, "equipment.delete", str(equipment.id), equipment.equipment_id)
    db.commit()
    db.refresh(equipment)
    return equipment


def get_stats(db: Session) -> EquipmentStats:
    base = db.query(Equipment).filter(Equipment.status != "DELETED")
    total = base.count()
    active = base.filter(Equipment.status == "ACTIVE").count()
    inactive = base.filter(Equipment.status == "INACTIVE").count()
    critical = base.filter(Equipment.criticality == "CRITICAL").count()
    under_maintenance = base.filter(Equipment.status == "UNDER_MAINTENANCE").count()
    decommissioned = base.filter(Equipment.status == "DECOMMISSIONED").count()
    return EquipmentStats(
        total=total,
        active=active,
        inactive=inactive,
        critical=critical,
        under_maintenance=under_maintenance,
        decommissioned=decommissioned,
    )
