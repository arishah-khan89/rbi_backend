"""Inspection tasks: planned inspection work per equipment. Everything is written by a person; the software works out only the
badge ("overdue", "due soon", "ok") and one limit: when a task has an RBI interval and a last date, its next date cannot be later
than the last date plus that interval (an earlier date is allowed)."""
import math
import uuid
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from app.core import audit
from app.crud.equipment import _same, _show
from app.crud.inspection_report import used_by_reports
from app.models.equipment import Equipment
from app.models.inspection_task import InspectionTask
from app.schemas.inspection_task import TaskCreate, TaskUpdate

DUE_SOON_DAYS = 90
FIELDS = ("name", "task_type", "details", "duration_days", "rbi_interval_years", "last_date", "next_date")
STATUSES = ("overdue", "due_soon", "ok", "none")


def due_status(next_date: Optional[date], today: Optional[date] = None) -> str:
    """Where a next date stands: no date, already past, within 90 days, or further away."""
    if next_date is None:
        return "none"
    today = today or date.today()
    if next_date < today:
        return "overdue"
    return "due_soon" if next_date <= today + timedelta(days=DUE_SOON_DAYS) else "ok"


def _status_sql(today: date):
    return case(
        (InspectionTask.next_date.is_(None), "none"),
        (InspectionTask.next_date < today, "overdue"),
        (InspectionTask.next_date <= today + timedelta(days=DUE_SOON_DAYS), "due_soon"),
        else_="ok",
    )


def _view(task: InspectionTask, equipment: Optional[Equipment]) -> InspectionTask:
    """The task with the extra lines the screens show (equipment tag and name, the badge)."""
    task.equipment_tag = equipment.equipment_id if equipment else None
    task.equipment_name = equipment.equipment_name if equipment else None
    task.status = due_status(task.next_date)
    return task


def _live_equipment(db: Session, equipment_id) -> Equipment:
    equipment = db.query(Equipment).filter(Equipment.id == equipment_id, Equipment.status != "DELETED").first()
    if equipment is None:
        raise ValueError("Choose an equipment that exists.")
    return equipment


def latest_next_date(last: Optional[date], years) -> Optional[date]:
    """The latest date the next inspection may be on: the last date plus the RBI interval (365.25 days a year, as the risk assessment counts)."""
    if last is None or years is None or Decimal(str(years)) <= 0:
        return None
    days = int((Decimal(str(years)) * Decimal("365.25")).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return last + timedelta(days=days)


def _check(db: Session, equipment_id, name: str, last: Optional[date], nxt: Optional[date], own_id=None,
           interval=None, check_limit: bool = False) -> None:
    if last and nxt and nxt < last:
        raise ValueError("The next date cannot be before the last date.")
    limit = latest_next_date(last, interval) if check_limit else None
    if limit and nxt and nxt > limit:
        raise ValueError(f"The next date cannot be later than the RBI date, {limit.isoformat()} (the last date plus the RBI interval of {Decimal(str(interval)).normalize():f} years). "
                         "An earlier date is allowed.")
    taken = db.query(InspectionTask.id).filter(
        InspectionTask.equipment_id == equipment_id, func.lower(InspectionTask.name) == name.lower())
    if own_id is not None:
        taken = taken.filter(InspectionTask.id != own_id)
    if taken.first() is not None:
        raise ValueError(f"This equipment already has a task called '{name}'. Use another name.")


def list_tasks(db: Session, equipment_id=None, task_type: Optional[str] = None, status: Optional[str] = None,
               q: Optional[str] = None, page: int = 1, page_size: int = 25) -> dict:
    today = date.today()
    query = db.query(InspectionTask, Equipment).join(Equipment, Equipment.id == InspectionTask.equipment_id) \
        .filter(Equipment.status != "DELETED")
    if equipment_id:
        query = query.filter(InspectionTask.equipment_id == equipment_id)
    if task_type:
        query = query.filter(InspectionTask.task_type == task_type)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(InspectionTask.name.ilike(like), InspectionTask.details.ilike(like),
                                 Equipment.equipment_id.ilike(like), Equipment.equipment_name.ilike(like)))

    st = _status_sql(today)
    counts = {s: 0 for s in STATUSES}
    for s, n in query.with_entities(st, func.count()).group_by(st).all():
        counts[s] = n                                    # the badges above the list ignore the status filter itself
    if status in STATUSES:
        query = query.filter(st == status)

    total = query.count()
    rows = query.order_by(InspectionTask.next_date.asc().nullslast(), func.lower(InspectionTask.name)) \
        .offset((page - 1) * page_size).limit(page_size).all()
    return {"data": [_view(t, e) for t, e in rows], "total": total, "page": page, "page_size": page_size,
            "total_pages": max(1, math.ceil(total / page_size)), "counts": counts}


def get_task(db: Session, task_id) -> Optional[InspectionTask]:
    row = db.query(InspectionTask, Equipment).join(Equipment, Equipment.id == InspectionTask.equipment_id) \
        .filter(InspectionTask.id == task_id).first()
    return _view(*row) if row else None


def create_task(db: Session, data: TaskCreate, actor: str = "System") -> InspectionTask:
    equipment = _live_equipment(db, data.equipment_id)
    _check(db, equipment.id, data.name, data.last_date, data.next_date, interval=data.rbi_interval_years, check_limit=True)
    task = InspectionTask(equipment_id=equipment.id, created_by=actor, updated_by=actor,
                          **data.model_dump(exclude={"equipment_id"}))
    db.add(task)
    db.flush()
    audit.record(db, actor, "task.create", str(task.id), f"{equipment.equipment_id} | {task.name} ({task.task_type})")
    db.commit()
    db.refresh(task)
    return _view(task, equipment)


def update_task(db: Session, task_id, data: TaskUpdate, actor: str = "System") -> Optional[InspectionTask]:
    task = db.query(InspectionTask).filter(InspectionTask.id == task_id).first()
    if task is None:
        return None
    equipment = db.query(Equipment).filter(Equipment.id == task.equipment_id).first()
    sent = data.model_dump(exclude_unset=True)
    if "name" in sent and not sent["name"]:
        raise ValueError("Give the task a name.")
    if "task_type" in sent and not sent["task_type"]:
        raise ValueError("Choose the type of the task.")
    changes = {f: (getattr(task, f), v) for f, v in sent.items() if f in FIELDS and not _same(getattr(task, f), v)}
    if task.source == "rbi" and "rbi_interval_years" in changes:
        raise ValueError("The RBI interval of this task comes from the approved risk assessment. Assess the component again to change it.")
    merged = {f: sent.get(f, getattr(task, f)) for f in ("name", "last_date", "next_date", "rbi_interval_years")}
    # the RBI limit is checked when the dates or the interval are changed (a task saved earlier with a later date can still have its other fields edited)
    _check(db, task.equipment_id, merged["name"], merged["last_date"], merged["next_date"], own_id=task.id,
           interval=merged["rbi_interval_years"], check_limit=bool({"last_date", "next_date", "rbi_interval_years"} & set(changes)))

    for f in changes:
        setattr(task, f, sent[f])
    if changes:
        task.updated_by = actor
        lines = "; ".join(f"{f}: {_show(old)} -> {_show(new)}" for f, (old, new) in changes.items())
        audit.record(db, actor, "task.edit", str(task.id), f"{equipment.equipment_id if equipment else '-'} | {task.name} | {lines}")
    db.commit()
    db.refresh(task)
    return _view(task, equipment)


def delete_task(db: Session, task_id, actor: str = "System") -> bool:
    task = db.query(InspectionTask).filter(InspectionTask.id == task_id).first()
    if task is None:
        return False
    used = used_by_reports(db, task.id)
    if used:
        raise ValueError(f"This task is covered by report {', '.join(used)}. Take it off the report first, then delete it.")
    equipment = db.query(Equipment).filter(Equipment.id == task.equipment_id).first()
    audit.record(db, actor, "task.delete", str(task.id), f"{equipment.equipment_id if equipment else '-'} | {task.name} ({task.task_type})")
    db.delete(task)
    db.commit()
    return True
