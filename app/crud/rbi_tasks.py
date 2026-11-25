"""Risk assessment (RBI) -> inspection tasks.

While an assessment is a draft the person chooses, for the Internal and for the External inspection plan, what happens when it is approved:
  new   an inspection task of this component is made ("RBI Internal inspection - <component>"), or, when an earlier revision already made
        one, that task is updated;
  link  a task the equipment already has (of the same type) takes the plan: its RBI interval, last date and next date come from the
        approved assessment and later revisions update it (its name, duration and details stay the person's);
  none  nothing is added (the default: nothing happens unless the person chose it).
The admin who approves sees the choice. Nothing here is calculated: the interval and the dates are the risk assessment's own result.
"""
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional
import uuid

from sqlalchemy.orm import Session

from app.core import audit
from app.models.inspection_task import InspectionTask
from app.models.rbi_assessment import RbiAssessment, RbiComponent

PLAN = (("Internal", "internal"), ("External", "external"))
ACTIONS = ("none", "new", "link")


def _day(value) -> Optional[date]:
    if not value:
        return None
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def plan_of(results: Optional[dict], key: str) -> Dict:
    """The inspection plan of one side ('internal' / 'external') in a finalized result: interval in years, last and next date."""
    planning = ((results or {}).get("result") or {}).get("planning") or {}
    due = planning.get(key) or {}
    years = planning.get(f"{key}_interval_years")
    return {"years": Decimal(str(years)) if years is not None else None, "last": _day(due.get("last")), "due": _day(due.get("due"))}


def _planned_date(existing: Optional[date], last: Optional[date], due: Optional[date]) -> Optional[date]:
    """The next date a task keeps when a newer assessment is approved. The person's own planned date stays when it still fits the new
    RBI date (not before the last date, not later than the RBI date); otherwise it becomes the RBI date. A task without a date gets the RBI date."""
    if existing is None:
        return due
    if due is None:
        return existing
    if (last is None or existing >= last) and existing <= due:
        return existing
    return due


def _free_name(db: Session, equipment_id, wanted: str, own_id=None) -> str:
    name, n = wanted[:200], 1
    while True:
        taken = db.query(InspectionTask.id).filter(InspectionTask.equipment_id == equipment_id, InspectionTask.name.ilike(name))
        if own_id is not None:
            taken = taken.filter(InspectionTask.id != own_id)
        if taken.first() is None:
            return name
        n += 1
        name = f"{wanted[:190]} ({n})"


def _candidates(db: Session, eq, comp: RbiComponent, task_type: str) -> List[InspectionTask]:
    """The tasks of this equipment of this type that a plan can be linked to: not already made from another component's assessment."""
    rows = db.query(InspectionTask).filter(InspectionTask.equipment_id == eq.id, InspectionTask.task_type == task_type).order_by(InspectionTask.name).all()
    return [t for t in rows if not (t.source == "rbi" and t.rbi_component_id not in (None, comp.id))]


def options(db: Session, eq, comp: RbiComponent) -> Dict[str, List[dict]]:
    """For the screen: the tasks each side can be linked to, and the one this component already made (if any)."""
    out = {}
    for task_type, key in PLAN:
        out[key] = [{"id": str(t.id), "name": t.name, "own": t.source == "rbi" and t.rbi_component_id == comp.id}
                    for t in _candidates(db, eq, comp, task_type)]
    return out


def clean_plan(db: Session, raw, eq, comp: RbiComponent) -> Dict[str, dict]:
    """What the person chose, checked: an action for each side, and for 'link' a task of this equipment of the right type."""
    raw = raw if isinstance(raw, dict) else {}
    out: Dict[str, dict] = {}
    for task_type, key in PLAN:
        choice = raw.get(key) if isinstance(raw.get(key), dict) else {}
        action = str(choice.get("action") or "none")
        if action not in ACTIONS:
            raise ValueError(f"Choose what happens to the {task_type.lower()} inspection plan: add a new task, link an existing task, or do not add it.")
        entry: Dict = {"action": action}
        if action == "link":
            try:
                task_id = uuid.UUID(str(choice.get("task_id")))
            except (ValueError, TypeError):
                raise ValueError(f"Choose the {task_type.lower()} task the plan is linked to.")
            if task_id not in {t.id for t in _candidates(db, eq, comp, task_type)}:
                raise ValueError(f"The {task_type.lower()} task you chose cannot be used: choose an {task_type.lower()} task of this equipment that is not taken by another component.")
            entry["task_id"] = str(task_id)
        out[key] = entry
    return out


def describe(db: Session, plan: Optional[dict]) -> Dict[str, dict]:
    """The plan with the names of its tasks, for the screen."""
    out: Dict[str, dict] = {}
    for _, key in PLAN:
        entry = dict((plan or {}).get(key) or {"action": "none"})
        if entry.get("task_id"):
            task = db.query(InspectionTask).filter(InspectionTask.id == entry["task_id"]).first()
            entry["task_name"] = task.name if task else None
        out[key] = entry
    return out


def summary(plan: Optional[dict]) -> str:
    return ", ".join(f"{key} {((plan or {}).get(key) or {}).get('action', 'none')}" for _, key in PLAN)


def reflect(db: Session, a: RbiAssessment, comp: RbiComponent, eq, actor: str) -> List[str]:
    """Does what the plan says for the approved assessment `a`. Does not commit. Returns what was done (and records it in the plan)."""
    plan = {k: dict(v) for k, v in (a.task_plan or {}).items()}
    result = (a.results or {}).get("result") or {}
    risk = result.get("risk") or {}
    planning = result.get("planning") or {}
    details = (f"From the risk assessment of {comp.name} (revision {a.revision}): {risk.get('level', '-')} risk, "
               f"inspection priority {risk.get('priority', '-')}. {planning.get('required_action') or ''}").strip()
    done: List[str] = []
    for task_type, key in PLAN:
        entry = plan.get(key) or {"action": "none"}
        action = entry.get("action", "none")
        found = plan_of(a.results, key)
        if action == "none" or found["years"] is None:
            continue
        values = {"rbi_interval_years": found["years"], "last_date": found["last"], "next_date": found["due"], "rbi_assessment_id": a.id}
        task: Optional[InspectionTask] = None
        if action == "link":
            task = db.query(InspectionTask).filter(InspectionTask.id == entry.get("task_id"), InspectionTask.equipment_id == eq.id).first()
        if action == "new" or task is None:
            task = db.query(InspectionTask).filter(InspectionTask.source == "rbi", InspectionTask.rbi_component_id == comp.id,
                                                   InspectionTask.task_type == task_type).first()
        if task is None:
            task = InspectionTask(equipment_id=eq.id, source="rbi", rbi_component_id=comp.id, task_type=task_type, details=details[:4000],
                                  name=_free_name(db, eq.id, f"RBI {task_type} inspection - {comp.name}"), created_by=actor, updated_by=actor, **values)
            db.add(task)
            db.flush()
            what = "created"
            audit.record(db, actor, "task.rbi_create", str(task.id),
                         f"{eq.equipment_id} | {task.name} | {found['years']} yrs | from the risk assessment, revision {a.revision}")
        else:
            linked = task.source != "rbi" or task.rbi_component_id is None
            values["next_date"] = _planned_date(task.next_date, found["last"], found["due"])           # the person's planned date is not overwritten
            for field, value in values.items():
                setattr(task, field, value)
            if linked:                                       # a task the person made: it keeps its name, duration and details
                task.source, task.rbi_component_id = "rbi", comp.id
            else:
                task.details = details[:4000]
            task.updated_by = actor
            what = "linked" if linked else "updated"
            audit.record(db, actor, "task.rbi_link" if linked else "task.rbi_update", str(task.id),
                         f"{eq.equipment_id} | {task.name} | {found['years']} yrs | from the risk assessment, revision {a.revision}")
        entry.update({"task_id": str(task.id), "done": what})
        plan[key] = entry
        done.append(f"{task_type}: task '{task.name}' {what}, every {found['years']} years")
    a.task_plan = plan
    return done


def suggestion(db: Session, equipment_id, task_type: str) -> Optional[dict]:
    """What the approved risk assessments of an equipment say for an Internal / External task: the shortest recommended interval
    (the most careful component), with its last and next date. None when there is nothing approved (or the type is not Internal / External)."""
    key = dict(PLAN).get(task_type)
    if key is None:
        return None
    rows = (db.query(RbiAssessment, RbiComponent).join(RbiComponent, RbiComponent.id == RbiAssessment.component_id)
            .filter(RbiComponent.equipment_id == equipment_id, RbiAssessment.state == "approved").all())
    best = None
    for a, comp in rows:
        plan = plan_of(a.results, key)
        if plan["years"] is None:
            continue
        if best is None or plan["years"] < best["interval_years"]:
            best = {"interval_years": plan["years"], "last_date": plan["last"], "next_date": plan["due"],
                    "component": comp.name, "revision": a.revision, "assessment_id": str(a.id)}
    return best
