import functools
import json
from contextvars import ContextVar
from typing import Optional

from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import uuid

from app.core import audit
from app.core.approval import LOCKED, NotOwnerError, RecordLockedError, locked_message, ownership_problem
from app.models.inspection import InspectionRecord
from app.models.equipment import Equipment
from app.schemas.inspection import InspectionRecordCreate


# When an import is running, the ids of every inspection it creates or changes are collected here, so the import
# can send exactly those to a reviewer (see api/endpoints/inspection.py).
IMPORT_COLLECTOR: ContextVar[Optional[list]] = ContextVar("import_collector", default=None)
# who is running an import (the importers build the records themselves, without a signed-in user)
IMPORT_ACTOR: ContextVar[Optional[str]] = ContextVar("import_actor", default=None)


def _actor(data) -> str:
    """The signed-in person behind this save (the endpoints put the username in `created_by`)."""
    by = getattr(data, "created_by", None)
    return by if by and by != "System" else (IMPORT_ACTOR.get() or "System")


def _num(v) -> str:
    if v is None:
        return "empty"
    s = f"{v:f}" if not isinstance(v, str) else v
    return s.rstrip("0").rstrip(".") if "." in s else s


def _zone_map(zones) -> dict:
    return {z.tml_code: [z.reading_1, z.reading_2, z.reading_3, z.reading_4] for z in (zones or [])}


def _describe_changes(before_zones, after_zones, before_date, after_date, before_remarks, after_remarks) -> list:
    """What an edit changed, in words: dates, zones added or removed, every reading that moved (old -> new)."""
    lines = []
    if before_date.date() != after_date.date():
        lines.append(f"date {before_date:%Y-%m-%d} -> {after_date:%Y-%m-%d}")
    old, new = _zone_map(before_zones), _zone_map(after_zones)
    for code in new.keys() - old.keys():
        lines.append(f"{code} added: " + ", ".join(_num(v) for v in new[code] if v is not None))
    for code in old.keys() - new.keys():
        lines.append(f"{code} removed (was " + ", ".join(_num(v) for v in old[code] if v is not None) + ")")
    for code in sorted(old.keys() & new.keys()):
        for i, (a, b) in enumerate(zip(old[code], new[code]), start=1):
            if (a is None) != (b is None) or (a is not None and a != b):
                lines.append(f"{code} reading {i}: {_num(a)} -> {_num(b)}")
    if (before_remarks or "") != (after_remarks or ""):
        lines.append("remarks changed")
    return lines


def _log(db: Session, actor: str, action: str, record, detail: str) -> None:
    text = detail if len(detail) <= 900 else detail[:900] + " ..."
    audit.record(db, actor, action, str(record.id), f"{record.equipment_tag} {record.inspection_date:%Y-%m-%d} | {text}")
    db.commit()


def _collects(fn):
    @functools.wraps(fn)
    def inner(*args, **kwargs):
        record = fn(*args, **kwargs)
        collector = IMPORT_COLLECTOR.get()
        if collector is not None and record is not None:
            collector.append(record.id)
        return record
    return inner


_LEGACY_ZONE_CODES = {
    "s1": ("S-1", "S1", "SHELL-1", "SHELL 1"),
    "s2": ("S-2", "S2", "SHELL-2", "SHELL 2"),
    "d1": ("D-1", "D1", "DISH-1", "DISH 1", "HEAD-1", "HEAD 1", "UPPE DISH", "UPPER DISH"),
    "d2": ("D-2", "D2", "DISH-2", "DISH 2", "HEAD-2", "HEAD 2", "LOWER DISH", "BOTTOM DISH"),
}
_LEGACY_POINTS = ("n", "s", "e", "w")
_LEGACY_ZONES = [   # code, name, type, field prefix, sort order (used when a record has no dynamic zones)
    ("S-1", "Shell Course 1", "Shell", "s1", 0),
    ("S-2", "Shell Course 2", "Shell", "s2", 1),
    ("D-1", "Upper Dish Head", "Dish", "d1", 2),
    ("D-2", "Lower Dish Head", "Dish", "d2", 3),
]


def _collect_readings(data) -> list:
    """Every non-empty thickness of a submitted inspection (dynamic zones first, else the 16 legacy fields)."""
    readings = [
        v for tml_in in (data.tml_readings or [])
        for v in (tml_in.reading_1, tml_in.reading_2, tml_in.reading_3, tml_in.reading_4) if v is not None
    ]
    if not readings:
        readings = [getattr(data, f"{z}_{p}") for z in _LEGACY_ZONE_CODES for p in _LEGACY_POINTS
                    if getattr(data, f"{z}_{p}") is not None]
    return readings


def _legacy_fields(data) -> dict:
    """The 16 legacy columns (s1_n ... d2_w): the submitted values, overridden by a dynamic zone whose
    code matches (S-1, Shell 2, D-1, Upper Dish ...)."""
    fields = {f"{z}_{p}": getattr(data, f"{z}_{p}") for z in _LEGACY_ZONE_CODES for p in _LEGACY_POINTS}
    for tml_in in data.tml_readings or []:
        code = (tml_in.tml_code or "").strip().upper()
        for z, codes in _LEGACY_ZONE_CODES.items():
            if code in codes:
                values = (tml_in.reading_1, tml_in.reading_2, tml_in.reading_3, tml_in.reading_4)
                for p, v in zip(_LEGACY_POINTS, values):
                    fields[f"{z}_{p}"] = v
                break
    return fields


def _sync_cml_readings(db: Session, equipment: Equipment, data: InspectionRecordCreate, tag: str,
                       create_missing_legacy_zones: bool) -> None:
    """Write the submitted readings into the CML tables (zones + readings) and recalculate the equipment.

    Dynamic zones: the zone is created when missing, its name / labels / t_min are kept in step with the
    Equipment Profile, and the reading is added (the CML engine calculates CR and remaining life).
    Legacy four-zone records: zones are only created when `create_missing_legacy_zones` (new records)."""
    from app.models.cml import InspectionTML
    from app.schemas.cml import InspectionReadingCreate
    from app.crud.cml import add_reading_to_tml, recalculate_equipment_cmls, min_t_for_type, is_zone_specific_type

    def reading_for(vals):
        return InspectionReadingCreate(
            inspection_date=data.inspection_date,
            inspector_name=data.inspector_name or "Lead Inspector",
            inspection_type=data.inspection_type or "Ultrasonic Thickness Measurement (UTM)",
            unit=data.thickness_unit or "mm",
            reading_1=vals[0], reading_2=vals[1], reading_3=vals[2], reading_4=vals[3],
            notes=data.remarks,
        )

    def find_zone(code):
        return db.query(InspectionTML).filter(
            InspectionTML.equipment_id == equipment.id, InspectionTML.tml_code == code).first()

    def new_zone(code, name, tml_type, labels, min_req, sort_order):
        zone = InspectionTML(equipment_id=equipment.id, equipment_tag=tag, tml_code=code, tml_name=name,
                             tml_type=tml_type, direction_labels=labels, min_required_thickness=min_req,
                             sort_order=sort_order)
        db.add(zone)
        db.commit()
        db.refresh(zone)
        return zone

    if data.tml_readings:
        for idx, tml_in in enumerate(data.tml_readings):
            vals = [tml_in.reading_1, tml_in.reading_2, tml_in.reading_3, tml_in.reading_4]
            if all(v is None for v in vals):
                continue
            zone = find_zone(tml_in.tml_code)
            if not zone:
                zone = new_zone(tml_in.tml_code, tml_in.tml_name or tml_in.tml_code, tml_in.tml_type or "Shell",
                                tml_in.direction_labels or "N,S,E,W",
                                min_t_for_type(equipment, tml_in.tml_type, tml_in.min_required_thickness), idx)
            else:
                if tml_in.direction_labels and zone.direction_labels != tml_in.direction_labels:
                    zone.direction_labels = tml_in.direction_labels
                if tml_in.tml_name and zone.tml_name != tml_in.tml_name:
                    zone.tml_name = tml_in.tml_name
                # keep the zone's t_min in step with the Equipment Profile (Nozzle / Other: the value entered on the zone)
                if is_zone_specific_type(zone.tml_type):
                    if tml_in.min_required_thickness is not None:
                        zone.min_required_thickness = tml_in.min_required_thickness
                else:
                    zone.min_required_thickness = min_t_for_type(equipment, zone.tml_type)
                db.commit()
            add_reading_to_tml(db, zone.id, reading_for(vals))
    else:
        for code, name, tml_type, prefix, sort_order in _LEGACY_ZONES:
            vals = [getattr(data, f"{prefix}_{p}") for p in _LEGACY_POINTS]
            if all(v is None for v in vals):
                continue
            zone = find_zone(code)
            if not zone and create_missing_legacy_zones:
                zone = new_zone(code, name, tml_type, "N,S,E,W", min_t_for_type(equipment, tml_type), sort_order)
            if zone:
                add_reading_to_tml(db, zone.id, reading_for(vals))
    recalculate_equipment_cmls(db, equipment.id)


def _snapshot_cml(db: Session, equipment_id):
    """Ids of the zones / readings that exist before a save, so a failed save can be undone."""
    from app.models.cml import InspectionTML, InspectionReading
    tml_ids = {t.id for t in db.query(InspectionTML.id).filter(InspectionTML.equipment_id == equipment_id)}
    reading_ids = set()
    if tml_ids:
        reading_ids = {r.id for r in db.query(InspectionReading.id).filter(InspectionReading.tml_id.in_(tml_ids))}
    return tml_ids, reading_ids


def _undo_created_record(db: Session, record, equipment_id, snapshot):
    """Remove everything a failed create produced (record, new readings, new zones).
    `record` is None when the readings were being added to an inspection that already existed: that one stays."""
    from app.models.cml import InspectionTML, InspectionReading
    db.rollback()
    pre_tmls, pre_readings = snapshot
    all_tmls = [t.id for t in db.query(InspectionTML.id).filter(InspectionTML.equipment_id == equipment_id)]
    if all_tmls:
        q = db.query(InspectionReading).filter(InspectionReading.tml_id.in_(all_tmls))
        if pre_readings:
            q = q.filter(~InspectionReading.id.in_(pre_readings))
        q.delete(synchronize_session=False)
    q = db.query(InspectionTML).filter(InspectionTML.equipment_id == equipment_id)
    if pre_tmls:
        q = q.filter(~InspectionTML.id.in_(pre_tmls))
    q.delete(synchronize_session=False)
    if record is not None:
        db.query(InspectionRecord).filter(InspectionRecord.id == record.id).delete(synchronize_session=False)
    db.commit()
    try:
        from app.crud.cml import recalculate_equipment_cmls
        recalculate_equipment_cmls(db, equipment_id)
    except Exception:
        db.rollback()


def _submitted_zone_codes(data) -> list:
    """Codes of the zones that carry at least one reading in this submission."""
    if data.tml_readings:
        return [t.tml_code for t in data.tml_readings
                if any(v is not None for v in (t.reading_1, t.reading_2, t.reading_3, t.reading_4))]
    return [code for code, _name, _type, prefix, _order in _LEGACY_ZONES
            if any(getattr(data, f"{prefix}_{p}") is not None for p in _LEGACY_POINTS)]


def _zones_already_read_on(db: Session, equipment_id, codes, start_of_day, end_of_day) -> list:
    """Of these zone codes, the ones that already have a reading on that day."""
    from app.models.cml import InspectionTML, InspectionReading
    taken = []
    for code in codes:
        zone = db.query(InspectionTML).filter(InspectionTML.equipment_id == equipment_id, InspectionTML.tml_code == code).first()
        if zone is None:
            continue
        if db.query(InspectionReading).filter(
            InspectionReading.tml_id == zone.id,
            InspectionReading.inspection_date >= start_of_day,
            InspectionReading.inspection_date <= end_of_day,
        ).first() is not None:
            taken.append(code)
    return taken


def _error_detail(e: Exception) -> str:
    """The technical text of an error (it can contain SQL and table names) is shown only in development;
    it is always written to the server log."""
    from app.core.config import settings
    return f" ({e})" if settings.APP_ENV == "development" else ""


def _add_zones_to_existing_inspection(db: Session, equipment: Equipment, record: InspectionRecord,
                                      data: InspectionRecordCreate) -> InspectionRecord:
    """Zones that have no reading yet on the date of an existing inspection are added to THAT inspection (one
    inspection per equipment and date): the readings go into the CML tables, the record's lowest reading is updated."""
    tag = data.equipment_tag or equipment.equipment_id
    snapshot = _snapshot_cml(db, data.equipment_id)
    try:
        _sync_cml_readings(db, equipment, data, tag, create_missing_legacy_zones=True)
    except Exception as e:
        import logging
        logging.getLogger("ams.inspection").exception("CML sync failed while adding zones to an inspection")
        _undo_created_record(db, None, data.equipment_id, snapshot)
        raise RuntimeError(f"Calculation could not be completed{_error_detail(e)}. Nothing was saved.") from e

    db.refresh(record)
    new_lowest = min(_collect_readings(data))
    record.min_measured_thickness = new_lowest if record.min_measured_thickness is None else min(record.min_measured_thickness, new_lowest)
    for field, value in _legacy_fields(data).items():
        if value is not None:
            setattr(record, field, value)
    if data.remarks and data.remarks not in (record.remarks or ""):
        record.remarks = f"{record.remarks} | {data.remarks}" if record.remarks else data.remarks
    db.commit()
    db.refresh(record)
    record.tml_readings = populate_record_tml_readings(db, record)
    _log(db, _actor(data), "inspection.add_zones", record, "zones added: " + ", ".join(_submitted_zone_codes(data)))
    return record


def create_inspection_record(db: Session, data: InspectionRecordCreate) -> InspectionRecord:
    # Fetch parent equipment for nominal and min required thickness
    equipment = db.query(Equipment).filter(Equipment.id == data.equipment_id).first()
    if not equipment:
        raise ValueError("Target equipment not found")

    # An inspection without a single thickness reading is not an inspection (an empty form must not create a record)
    readings = _collect_readings(data)
    if not readings:
        raise ValueError("Enter at least one thickness reading. Nothing was saved.")

    # One inspection record per equipment and date: a second one is refused, the existing one is edited instead
    insp_date = data.inspection_date.date() if isinstance(data.inspection_date, datetime) else data.inspection_date
    start_of_day = datetime.combine(insp_date, datetime.min.time())
    end_of_day = datetime.combine(insp_date, datetime.max.time())

    existing = db.query(InspectionRecord).filter(
        InspectionRecord.equipment_id == data.equipment_id,
        InspectionRecord.inspection_date >= start_of_day,
        InspectionRecord.inspection_date <= end_of_day
    ).first()
    if existing:
        if existing.approval_status in LOCKED:
            raise RecordLockedError(locked_message(existing))
        problem = ownership_problem(existing, _actor(data))
        if problem:
            raise NotOwnerError(problem)
        formatted_date = insp_date.strftime('%d-%b-%Y') if hasattr(insp_date, 'strftime') else str(insp_date)
        taken = _zones_already_read_on(db, data.equipment_id, _submitted_zone_codes(data), start_of_day, end_of_day)
        if taken:
            raise ValueError(
                f"Zone(s) {', '.join(taken)} of {equipment.equipment_id} already have readings on {formatted_date}. "
                f"To change them open that inspection with Edit. Nothing was saved."
            )
        return _add_zones_to_existing_inspection(db, equipment, existing, data)

    # Corrosion rate and remaining life are NOT estimated here. They are calculated by the
    # CML engine (client sheet method: Ti / Tp / Tf, MAX(short-term, long-term) and the
    # t_min stored in the Equipment Profile) and written back by recalculate_equipment_cmls().
    # A first/baseline reading has no corrosion rate yet, so these stay empty (None).
    min_measured_t = min(readings) if readings else None
    corrosion_rate = None
    remaining_life = None

    legacy = _legacy_fields(data)

    # Next inspection dates (API 510 internal / external) are set by recalculate_equipment_cmls()
    # once a remaining life can be calculated; nothing is assumed for a baseline reading.
    next_inspection_due = None

    record = InspectionRecord(
        equipment_id=data.equipment_id,
        equipment_tag=data.equipment_tag or equipment.equipment_id,
        inspection_date=data.inspection_date,
        inspector_name=data.inspector_name or "Lead Inspector",
        inspection_type=data.inspection_type or "Ultrasonic Thickness Measurement (UTM)",
        thickness_unit=data.thickness_unit or "mm",
        **legacy,
        min_measured_thickness=min_measured_t,
        corrosion_rate=corrosion_rate,
        remaining_life=remaining_life,
        next_inspection_due=next_inspection_due,
        remarks=data.remarks,
        created_by=_actor(data),      # the signed-in person; an import belongs to the person who ran it
    )

    cml_snapshot = _snapshot_cml(db, data.equipment_id)
    db.add(record)

    # Latest inspection date only; corrosion rate / remaining life come from the CML engine
    equipment.inspection_date_current = data.inspection_date
    for field, value in legacy.items():
        if value is not None:
            setattr(equipment, field, value)

    db.commit()
    db.refresh(record)

    # Sync into the CML tables (zones + readings) and recalculate the equipment
    try:
        _sync_cml_readings(db, equipment, data, data.equipment_tag or equipment.equipment_id,
                           create_missing_legacy_zones=True)
    except Exception as e:
        # Never report success when the calculation could not be saved: undo the new record
        import logging
        logging.getLogger("ams.inspection").exception("CML sync failed while creating an inspection")
        _undo_created_record(db, record, data.equipment_id, cml_snapshot)
        raise RuntimeError(f"Calculation could not be completed{_error_detail(e)}. The inspection was not saved.") from e

    record.tml_readings = populate_record_tml_readings(db, record)
    _log(db, _actor(data), "inspection.create", record, f"{len(record.tml_readings)} zone(s), lowest reading {_num(record.min_measured_thickness)}")
    return record


def _fmt_t(value) -> str:
    return "not set" if value is None else format(Decimal(str(value)).normalize(), "f")


def take_snapshot(db: Session, record: InspectionRecord) -> str:
    """The t_min of every zone of this inspection right now (JSON). Taken when it is locked."""
    zones = {z.tml_code: (None if z.min_required_thickness is None else str(z.min_required_thickness))
             for z in _live_tml_readings(db, record)}
    return json.dumps({"taken_at": datetime.now(timezone.utc).isoformat(), "zones": zones})


def ensure_locked_snapshots(db: Session, equipment_id) -> None:
    """Locked inspections of an old kind have no snapshot yet: take it now, before the equipment data changes."""
    changed = False
    for rec in db.query(InspectionRecord).filter(InspectionRecord.equipment_id == equipment_id,
                                                 InspectionRecord.approval_status.in_(LOCKED),
                                                 InspectionRecord.locked_snapshot.is_(None)).all():
        rec.locked_snapshot = take_snapshot(db, rec)
        changed = True
    if changed:
        db.commit()


def populate_record_tml_readings(db: Session, record: InspectionRecord):
    """The zones and readings of this inspection. While it is locked (waiting for approval or approved) each zone shows
    the t_min it had when it was locked; `record.t_min_note` says so when the equipment's t_min has changed since."""
    zones = _live_tml_readings(db, record)
    record.t_min_note = None
    if record.approval_status in LOCKED and record.locked_snapshot:
        try:
            frozen = json.loads(record.locked_snapshot).get("zones", {})
        except (ValueError, AttributeError):
            frozen = {}
        moved = []
        for z in zones:
            if z.tml_code in frozen:
                was = frozen[z.tml_code]
                was_d = None if was is None else Decimal(was)
                if was_d != z.min_required_thickness:
                    moved.append(f"{z.tml_code}: {_fmt_t(was_d)} mm then, {_fmt_t(z.min_required_thickness)} mm now")
                z.min_required_thickness = was_d
        if moved:
            record.t_min_note = ("The minimum thickness (t_min) has changed since this inspection was locked (" + "; ".join(moved) +
                                 "). It keeps the values it was locked with; the equipment's current life uses the new t_min.")
    return zones


def _live_tml_readings(db: Session, record: InspectionRecord):
    """Load all TML measurement zones and readings matching this inspection record (with the t_min they have today)."""
    from app.models.cml import InspectionTML, InspectionReading
    from app.schemas.inspection import DynamicTMLReadingInput

    d_start = record.inspection_date - timedelta(hours=12)
    d_end = record.inspection_date + timedelta(hours=12)
    readings = db.query(InspectionReading).join(InspectionTML).filter(
        InspectionTML.equipment_id == record.equipment_id,
        InspectionReading.inspection_date >= d_start,
        InspectionReading.inspection_date <= d_end
    ).order_by(InspectionTML.sort_order.asc(), InspectionTML.created_at.asc()).all()

    tml_list = []
    if readings:
        for rd in readings:
            tml = rd.tml
            tml_list.append(DynamicTMLReadingInput(
                tml_id=tml.id,
                tml_code=tml.tml_code,
                tml_name=tml.tml_name or tml.tml_code,
                tml_type=tml.tml_type or "Shell",
                direction_labels=tml.direction_labels or "N,S,E,W",
                reading_1=rd.reading_1,
                reading_2=rd.reading_2,
                reading_3=rd.reading_3,
                reading_4=rd.reading_4,
                reading_5=rd.reading_5,
                reading_6=rd.reading_6,
                min_required_thickness=tml.min_required_thickness,      # so Edit shows the same t_min as the new-inspection form
            ))
    else:
        legacy_zones = [
            ("S-1", "Shell Course 1", "Shell", [record.s1_n, record.s1_s, record.s1_e, record.s1_w]),
            ("S-2", "Shell Course 2", "Shell", [record.s2_n, record.s2_s, record.s2_e, record.s2_w]),
            ("D-1", "Upper Dish Head", "Dish", [record.d1_n, record.d1_s, record.d1_e, record.d1_w]),
            ("D-2", "Lower Dish Head", "Dish", [record.d2_n, record.d2_s, record.d2_e, record.d2_w]),
        ]
        for code, name, t_type, vals in legacy_zones:
            if any(v is not None for v in vals):
                tml_list.append(DynamicTMLReadingInput(
                    tml_code=code,
                    tml_name=name,
                    tml_type=t_type,
                    direction_labels="N,S,E,W",
                    reading_1=vals[0],
                    reading_2=vals[1],
                    reading_3=vals[2],
                    reading_4=vals[3],
                ))

    return tml_list


def get_inspection_record(db: Session, record_id: uuid.UUID):
    record = db.query(InspectionRecord).filter(InspectionRecord.id == record_id).first()
    if record:
        record.tml_readings = populate_record_tml_readings(db, record)
    return record


def get_inspection_list(
    db: Session,
    equipment_tag: str = None,
    page: int = 1,
    page_size: int = 25,
    equipment_id=None,
):
    query = db.query(InspectionRecord)
    if equipment_id:
        # exact equipment (the history of ONE equipment); a tag filter is a "contains" search and can match others
        query = query.filter(InspectionRecord.equipment_id == equipment_id)
    if equipment_tag:
        query = query.filter(InspectionRecord.equipment_tag.ilike(f"%{equipment_tag}%"))

    total = query.count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    items = query.order_by(InspectionRecord.inspection_date.desc()).offset((page - 1) * page_size).limit(page_size).all()

    for item in items:
        item.tml_readings = populate_record_tml_readings(db, item)

    return items, total, total_pages


def get_inspection_stats(db: Session):
    # Total = logged inspection events. Healthy / warning / critical are counted per ASSET using the
    # equipment's governing remaining life (not once per historical record).
    total = db.query(InspectionRecord).count()
    assets = db.query(Equipment).filter(Equipment.status != "DELETED")
    critical = assets.filter(Equipment.remaining_life <= 2.0).count()
    warning = assets.filter(Equipment.remaining_life > 2.0, Equipment.remaining_life <= 5.0).count()
    healthy = assets.filter(Equipment.remaining_life > 5.0).count()

    return {
        "total_inspections": total,
        "critical_life_count": critical,
        "warning_life_count": warning,
        "healthy_life_count": healthy,
    }


def update_inspection_record(db: Session, record_id: uuid.UUID, data: InspectionRecordCreate) -> InspectionRecord:
    record = db.query(InspectionRecord).filter(InspectionRecord.id == record_id).first()
    if not record:
        raise ValueError("Inspection record not found")
    if record.approval_status in LOCKED:
        raise RecordLockedError(locked_message(record))
    problem = ownership_problem(record, _actor(data))
    if problem:
        raise NotOwnerError(problem)

    equipment = db.query(Equipment).filter(Equipment.id == record.equipment_id).first()
    if not equipment:
        raise ValueError("Target equipment not found")

    old_date = record.inspection_date
    before_zones = populate_record_tml_readings(db, record)
    before_remarks = record.remarks

    # If inspection_date is changed, check for duplicate on the new date
    insp_date = data.inspection_date.date() if isinstance(data.inspection_date, datetime) else data.inspection_date
    start_of_day = datetime.combine(insp_date, datetime.min.time())
    end_of_day = datetime.combine(insp_date, datetime.max.time())

    existing = db.query(InspectionRecord).filter(
        InspectionRecord.equipment_id == record.equipment_id,
        InspectionRecord.id != record_id,
        InspectionRecord.inspection_date >= start_of_day,
        InspectionRecord.inspection_date <= end_of_day
    ).first()

    if existing:
        formatted_date = insp_date.strftime('%d-%b-%Y') if hasattr(insp_date, 'strftime') else str(insp_date)
        raise ValueError(f"Another inspection record already exists for {equipment.equipment_id} on {formatted_date}.")

    readings = _collect_readings(data)
    if not readings:
        raise ValueError("An inspection needs at least one thickness reading. To remove the inspection use Delete. Nothing was changed.")

    # Corrosion rate and remaining life are NOT estimated here. They are calculated by the
    # CML engine (client sheet method: Ti / Tp / Tf, MAX(short-term, long-term) and the
    # t_min stored in the Equipment Profile) and written back by recalculate_equipment_cmls().
    # A first/baseline reading has no corrosion rate yet, so these stay empty (None).
    min_measured_t = min(readings) if readings else None
    corrosion_rate = None
    remaining_life = None

    legacy = _legacy_fields(data)

    # Update record
    record.inspection_date = data.inspection_date
    record.inspector_name = data.inspector_name or record.inspector_name
    record.inspection_type = data.inspection_type or record.inspection_type
    record.thickness_unit = data.thickness_unit or record.thickness_unit
    for field, value in legacy.items():
        setattr(record, field, value)
    record.min_measured_thickness = min_measured_t
    record.corrosion_rate = corrosion_rate
    record.remaining_life = remaining_life
    record.remarks = data.remarks
    if data.created_by:
        record.updated_by = data.created_by      # the signed-in user making this change

    db.commit()
    db.refresh(record)

    # Sync into the CML tables (zones + readings) and recalculate the equipment
    try:
        from app.models.cml import InspectionTML, InspectionReading

        # If inspection date was changed, remove readings tied to old date
        old_d = old_date.date() if isinstance(old_date, datetime) else old_date
        new_d = data.inspection_date.date() if isinstance(data.inspection_date, datetime) else data.inspection_date
        if old_date and old_d != new_d:
            d_start = old_date - timedelta(hours=12)
            d_end = old_date + timedelta(hours=12)
            old_readings = db.query(InspectionReading).join(InspectionTML).filter(
                InspectionTML.equipment_id == equipment.id,
                InspectionReading.inspection_date >= d_start,
                InspectionReading.inspection_date <= d_end
            ).all()
            for ord_item in old_readings:
                db.delete(ord_item)
            db.commit()

        _sync_cml_readings(db, equipment, data, equipment.equipment_id, create_missing_legacy_zones=False)
    except Exception as e:
        import logging
        logging.getLogger("ams.inspection").exception("CML sync failed while updating an inspection")
        db.rollback()
        raise RuntimeError(
            f"The readings were saved but the calculation could not be completed{_error_detail(e)}. "
            f"Open the record and save it again to retry."
        ) from e

    record.tml_readings = populate_record_tml_readings(db, record)
    changes = _describe_changes(before_zones, record.tml_readings, old_date, record.inspection_date, before_remarks, record.remarks)
    if changes:
        _log(db, _actor(data), "inspection.edit", record, "; ".join(changes))
    return record


def delete_inspection_record(db: Session, record_id: uuid.UUID, actor: str = "System") -> bool:
    """Delete an inspection record, its associated CML readings, and recalculate equipment rates."""
    record = db.query(InspectionRecord).filter(InspectionRecord.id == record_id).first()
    if not record:
        raise ValueError("Inspection record not found")
    if record.approval_status in LOCKED:
        raise RecordLockedError(locked_message(record))

    equipment_id = record.equipment_id
    insp_date = record.inspection_date
    deleted_label = f"{record.equipment_tag} {insp_date:%Y-%m-%d} (was {record.approval_status}, lowest reading {_num(record.min_measured_thickness)})"
    record_key = str(record.id)

    # Delete related inspection readings for this date and equipment
    try:
        from app.models.cml import InspectionTML, InspectionReading
        from app.crud.cml import recalculate_equipment_cmls

        tmls = db.query(InspectionTML).filter(InspectionTML.equipment_id == equipment_id).all()
        tml_ids = [t.id for t in tmls]
        if tml_ids and insp_date:
            date_val = insp_date.date() if hasattr(insp_date, 'date') else insp_date
            start_of_day = datetime.combine(date_val, datetime.min.time())
            end_of_day = datetime.combine(date_val, datetime.max.time())
            db.query(InspectionReading).filter(
                InspectionReading.tml_id.in_(tml_ids),
                InspectionReading.inspection_date >= start_of_day,
                InspectionReading.inspection_date <= end_of_day
            ).delete(synchronize_session=False)
            db.commit()

        # Clean up any orphaned TML zones that now have 0 readings left
        for t in tmls:
            reading_count = db.query(InspectionReading).filter(InspectionReading.tml_id == t.id).count()
            if reading_count == 0:
                db.delete(t)
        db.commit()
    except Exception as e:
        # do not delete the record while its readings could not be cleaned up
        import logging
        logging.getLogger("ams.inspection").exception("Cleaning up readings failed while deleting an inspection")
        db.rollback()
        raise RuntimeError(f"The inspection could not be deleted ({e}). Nothing was changed.") from e

    # Delete the inspection record
    db.delete(record)
    audit.record(db, actor, "inspection.delete", record_key, deleted_label)
    db.commit()

    # If no inspection records remain for this equipment, clean up and reset stats
    remaining_recs = db.query(InspectionRecord).filter(InspectionRecord.equipment_id == equipment_id).count()
    if remaining_recs == 0:
        db.query(InspectionTML).filter(InspectionTML.equipment_id == equipment_id).delete(synchronize_session=False)
        eq = db.query(Equipment).filter(Equipment.id == equipment_id).first()
        if eq:
            from app.crud.cml import reset_equipment_inspection_results
            reset_equipment_inspection_results(eq)
        db.commit()
    else:
        # Recalculate remaining life and corrosion rate for equipment with remaining records
        try:
            recalculate_equipment_cmls(db, equipment_id)
        except Exception as e:
            import logging
            logging.getLogger("ams.inspection").exception("Recalculation after deleting an inspection failed")
            db.rollback()
            raise RuntimeError(
                f"The inspection was deleted, but the remaining life could not be recalculated ({e}). "
                f"Open another inspection of this equipment and save it again to refresh the values."
            ) from e

    return True


# every importer goes through these two, so they are the places to note what was saved
create_inspection_record = _collects(create_inspection_record)
update_inspection_record = _collects(update_inspection_record)
