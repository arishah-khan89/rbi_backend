"""Inspection reports (tab "Inspection Report"): create, change, list, delete, and their files.
The approval steps (send, approve, return, withdraw, reopen) are in report_workflow.py.

Nothing here calculates anything: every value is written by a person. Approving a report does not change a task's dates.
"""
import math
from datetime import date
from typing import Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core import audit
from app.core.approval import APPROVED, DRAFT, EDITABLE, LOCKED, PENDING, RETURNED, NotOwnerError, RecordLockedError, owner_of
from app.crud.equipment import _same, _show
from app.models.equipment import Equipment
from app.models.inspection_report import (
    InspectionReport, InspectionReportFile, InspectionReportRecipient, InspectionReportTask)
from app.models.inspection_task import InspectionTask
from app.models.user import User
from app.schemas.inspection_report import ReportCreate, ReportUpdate

REJECTED = "rejected"          # the admin said no: the report is closed (write a new one if it must be reported again)
STATES = (DRAFT, PENDING, RETURNED, APPROVED, REJECTED)
MAX_FILES = 20
FIELDS = ("report_no", "inspection_date", "title", "reason", "summary")


# ---------------------------------------------------------------- who may do what to a report
def locked_message(report: InspectionReport) -> str:
    if report.approval_status == REJECTED:
        return f"Report {report.report_no} was rejected and is closed. Write a new report if the inspection must be reported again."
    if report.approval_status == APPROVED:
        return (f"Report {report.report_no} is approved and locked. The admin who approved it can reopen it "
                f"(with a reason) if it must be changed.")
    return f"Report {report.report_no} is waiting for approval and is locked. Withdraw it first if it must be changed."


def ownership_problem(report: InspectionReport, actor: Optional[str]) -> Optional[str]:
    """A sentence when `actor` may not change or send this draft / returned report. With sign-in off nobody is held back."""
    if not actor or actor.strip().lower() == "system" or report.approval_status not in EDITABLE:
        return None
    owner = owner_of(report)
    if owner and owner.lower() != actor.strip().lower():
        return f"Report {report.report_no} belongs to {owner}. Only {owner} can change it or send it for approval."
    return None


def ensure_editable(report: InspectionReport, actor: Optional[str]) -> None:
    if report.approval_status not in EDITABLE:                  # pending, approved and rejected reports are not changed
        raise RecordLockedError(locked_message(report))
    problem = ownership_problem(report, actor)
    if problem:
        raise NotOwnerError(problem)


# ---------------------------------------------------------------- checks
def _live_equipment(db: Session, equipment_id) -> Equipment:
    equipment = db.query(Equipment).filter(Equipment.id == equipment_id, Equipment.status != "DELETED").first()
    if equipment is None:
        raise ValueError("Choose an equipment that exists.")
    return equipment


def _check_report_no(db: Session, report_no: str, own_id=None) -> None:
    q = db.query(InspectionReport.id).filter(func.lower(InspectionReport.report_no) == report_no.lower())
    if own_id is not None:
        q = q.filter(InspectionReport.id != own_id)
    if q.first() is not None:
        raise ValueError(f"There is already a report with the number '{report_no}'. Report numbers are not repeated.")


def _check_tasks(db: Session, equipment_id, task_ids: list) -> list[InspectionTask]:
    ids = list(dict.fromkeys(task_ids))
    tasks = db.query(InspectionTask).filter(InspectionTask.id.in_(ids)).all() if ids else []
    if len(tasks) != len(ids):
        raise ValueError("One of the chosen tasks does not exist any more.")
    wrong = [t.name for t in tasks if t.equipment_id != equipment_id]
    if wrong:
        raise ValueError(f"A report can only cover the tasks of its own equipment. Not of this equipment: {', '.join(wrong)}.")
    return tasks


def _check_recipients(db: Session, usernames: list[str], actor: Optional[str]) -> list[str]:
    wanted = list(dict.fromkeys(u.strip().lower() for u in usernames if u and u.strip()))
    me = (actor or "").strip().lower()
    wanted = [u for u in wanted if u != me]                     # nobody is "sent" their own report
    found = {u.username for u in db.query(User).filter(User.username.in_(wanted), User.is_active.is_(True)).all()} if wanted else set()
    unknown = [u for u in wanted if u not in found]
    if unknown:
        raise ValueError(f"These people cannot be chosen (no such active user): {', '.join(unknown)}.")
    return wanted


def _task_names(db: Session, report_id) -> list[str]:
    return [n for (n,) in db.query(InspectionTask.name).join(InspectionReportTask, InspectionReportTask.task_id == InspectionTask.id)
            .filter(InspectionReportTask.report_id == report_id).order_by(InspectionTask.name).all()]


def _recipient_names(db: Session, report_id) -> list[str]:
    return sorted(u for (u,) in db.query(InspectionReportRecipient.username).filter(InspectionReportRecipient.report_id == report_id).all())


def _set_tasks(db: Session, report_id, tasks: list[InspectionTask]) -> None:
    db.query(InspectionReportTask).filter(InspectionReportTask.report_id == report_id).delete(synchronize_session=False)
    for t in tasks:
        db.add(InspectionReportTask(report_id=report_id, task_id=t.id))


def _set_recipients(db: Session, report_id, usernames: list[str]) -> None:
    db.query(InspectionReportRecipient).filter(InspectionReportRecipient.report_id == report_id).delete(synchronize_session=False)
    for u in usernames:
        db.add(InspectionReportRecipient(report_id=report_id, username=u))


# ---------------------------------------------------------------- what the screens receive
def decorate(db: Session, reports: list[InspectionReport], full: bool = False) -> list[InspectionReport]:
    """Adds the lines the screens show: equipment name, how many tasks / files, and (full) the tasks, files and recipients."""
    if not reports:
        return reports
    ids = [r.id for r in reports]
    names = dict(db.query(Equipment.id, Equipment.equipment_name).filter(Equipment.id.in_({r.equipment_id for r in reports})).all())
    task_n = dict(db.query(InspectionReportTask.report_id, func.count()).filter(InspectionReportTask.report_id.in_(ids))
                  .group_by(InspectionReportTask.report_id).all())
    file_n = dict(db.query(InspectionReportFile.report_id, func.count()).filter(InspectionReportFile.report_id.in_(ids))
                  .group_by(InspectionReportFile.report_id).all())
    for r in reports:
        r.equipment_name = names.get(r.equipment_id)
        r.task_count = task_n.get(r.id, 0)
        r.file_count = file_n.get(r.id, 0)
        if full:
            r.tasks = db.query(InspectionTask).join(InspectionReportTask, InspectionReportTask.task_id == InspectionTask.id) \
                .filter(InspectionReportTask.report_id == r.id).order_by(InspectionTask.name).all()
            r.files = db.query(InspectionReportFile).filter(InspectionReportFile.report_id == r.id) \
                .order_by(InspectionReportFile.uploaded_at).all()
            users = {u.username: u for u in db.query(User).filter(User.username.in_(_recipient_names(db, r.id))).all()}
            r.recipients = [{"username": n, "full_name": users[n].full_name if n in users else None,
                             "role": users[n].role if n in users else None} for n in _recipient_names(db, r.id)]
    return reports


def get_report(db: Session, report_id) -> Optional[InspectionReport]:
    report = db.query(InspectionReport).filter(InspectionReport.id == report_id).first()
    return decorate(db, [report], full=True)[0] if report else None


def list_reports(db: Session, equipment_id=None, status: Optional[str] = None, q: Optional[str] = None,
                 mine: Optional[str] = None, assigned_to: Optional[str] = None, page: int = 1, page_size: int = 25) -> dict:
    """`mine` = the reports this person wrote or sent; `assigned_to` = the reports waiting for this person's decision."""
    query = db.query(InspectionReport)
    if equipment_id:
        query = query.filter(InspectionReport.equipment_id == equipment_id)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(InspectionReport.report_no.ilike(like), InspectionReport.title.ilike(like),
                                 InspectionReport.summary.ilike(like), InspectionReport.reason.ilike(like),
                                 InspectionReport.equipment_tag.ilike(like)))
    if mine:
        query = query.filter(or_(InspectionReport.submitted_by.ilike(mine), InspectionReport.created_by.ilike(mine)))
    if assigned_to:
        query = query.filter(InspectionReport.approval_status == PENDING, InspectionReport.assigned_reviewer == assigned_to.lower())

    counts = {s: 0 for s in STATES}
    for s, n in query.with_entities(InspectionReport.approval_status, func.count()).group_by(InspectionReport.approval_status).all():
        counts[s] = n
    if status in STATES:
        query = query.filter(InspectionReport.approval_status == status)

    total = query.count()
    rows = query.order_by(InspectionReport.inspection_date.desc(), InspectionReport.report_no) \
        .offset((page - 1) * page_size).limit(page_size).all()
    return {"data": decorate(db, rows), "total": total, "page": page, "page_size": page_size,
            "total_pages": max(1, math.ceil(total / page_size)), "counts": counts}


def summary(db: Session, username: str, is_reviewer: bool) -> dict:
    base = db.query(InspectionReport)
    me = username.lower()
    return {
        "waiting_for_me": base.filter(InspectionReport.approval_status == PENDING, InspectionReport.assigned_reviewer == me).count() if is_reviewer else 0,
        "returned_to_me": base.filter(InspectionReport.approval_status == RETURNED, InspectionReport.submitted_by.ilike(username)).count(),
        "my_drafts": base.filter(InspectionReport.approval_status == DRAFT, or_(
            InspectionReport.submitted_by.ilike(username),
            InspectionReport.submitted_by.is_(None) & InspectionReport.created_by.ilike(username))).count(),
    }


def recipients_choice(db: Session) -> list[dict]:
    """Who can be chosen under "Send to": every active person (any signed-in person may ask; names only)."""
    return [{"username": u.username, "full_name": u.full_name, "role": u.role}
            for u in db.query(User).filter(User.is_active.is_(True)).order_by(User.username).all()]


def used_by_reports(db: Session, task_id) -> list[str]:
    """The numbers of the reports that cover this task (a task that a report covers cannot be deleted)."""
    return [no for (no,) in db.query(InspectionReport.report_no).join(InspectionReportTask, InspectionReportTask.report_id == InspectionReport.id)
            .filter(InspectionReportTask.task_id == task_id).order_by(InspectionReport.report_no).all()]


# ---------------------------------------------------------------- create / change / delete
def create_report(db: Session, data: ReportCreate, actor: str = "System") -> InspectionReport:
    equipment = _live_equipment(db, data.equipment_id)
    _check_report_no(db, data.report_no)
    tasks = _check_tasks(db, equipment.id, data.task_ids or [])
    recipients = _check_recipients(db, data.recipients or [], actor)
    report = InspectionReport(equipment_id=equipment.id, equipment_tag=equipment.equipment_id, report_no=data.report_no,
                              inspection_date=data.inspection_date, title=data.title, reason=data.reason, summary=data.summary,
                              approval_status=DRAFT, created_by=actor, updated_by=actor)
    db.add(report)
    db.flush()
    _set_tasks(db, report.id, tasks)
    _set_recipients(db, report.id, recipients)
    audit.record(db, actor, "report.create", str(report.id),
                 f"{equipment.equipment_id} | {report.report_no} | {report.title}, {report.inspection_date:%Y-%m-%d}")
    db.commit()
    return get_report(db, report.id)


def update_report(db: Session, report_id, data: ReportUpdate, actor: str = "System") -> Optional[InspectionReport]:
    report = db.query(InspectionReport).filter(InspectionReport.id == report_id).first()
    if report is None:
        return None
    ensure_editable(report, actor)
    sent = data.model_dump(exclude_unset=True)
    for f in ("report_no", "inspection_date", "title"):
        if f in sent and not sent[f]:
            raise ValueError(f"The report needs its {f.replace('_', ' ')}.")

    changes = {f: (getattr(report, f), sent[f]) for f in FIELDS if f in sent and not _same(getattr(report, f), sent[f])}
    if "report_no" in changes:
        _check_report_no(db, sent["report_no"], own_id=report.id)
    lines = [f"{f}: {_show(old)} -> {_show(new)}" for f, (old, new) in changes.items() if f not in ("reason", "summary")]
    lines += [f"{f} changed" for f in ("reason", "summary") if f in changes]          # long texts: only that they changed

    if sent.get("task_ids") is not None:
        before = _task_names(db, report.id)
        tasks = _check_tasks(db, report.equipment_id, sent["task_ids"])
        after = sorted(t.name for t in tasks)
        if before != after:
            _set_tasks(db, report.id, tasks)
            lines.append(f"tasks: {', '.join(before) or '-'} -> {', '.join(after) or '-'}")
    if sent.get("recipients") is not None:
        before = _recipient_names(db, report.id)
        after = sorted(_check_recipients(db, sent["recipients"], actor))
        if before != after:
            _set_recipients(db, report.id, after)
            lines.append(f"send to: {', '.join(before) or '-'} -> {', '.join(after) or '-'}")

    for f in changes:
        setattr(report, f, sent[f])
    if lines:
        report.updated_by = actor
        audit.record(db, actor, "report.edit", str(report.id), f"{report.equipment_tag} | {report.report_no} | " + "; ".join(lines))
    db.commit()
    return get_report(db, report.id)


def delete_report(db: Session, report_id, actor: str = "System") -> bool:
    report = db.query(InspectionReport).filter(InspectionReport.id == report_id).first()
    if report is None:
        return False
    if report.approval_status in LOCKED:
        raise RecordLockedError(locked_message(report))
    audit.record(db, actor, "report.delete", str(report.id), f"{report.equipment_tag} | {report.report_no} | {report.title}")
    db.delete(report)
    db.commit()
    return True


# ---------------------------------------------------------------- files
def add_file(db: Session, report_id, filename: str, content: bytes, content_type: Optional[str], actor: str = "System") -> InspectionReportFile:
    report = db.query(InspectionReport).filter(InspectionReport.id == report_id).first()
    if report is None:
        raise LookupError("Inspection report not found")
    ensure_editable(report, actor)
    if db.query(InspectionReportFile).filter(InspectionReportFile.report_id == report.id).count() >= MAX_FILES:
        raise ValueError(f"A report can have at most {MAX_FILES} files.")
    file = InspectionReportFile(report_id=report.id, filename=filename, content_type=content_type, size_bytes=len(content),
                                content=content, uploaded_by=actor)
    db.add(file)
    db.flush()
    audit.record(db, actor, "report.file_add", str(report.id), f"{report.equipment_tag} | {report.report_no} | {filename} ({len(content) // 1024} KB)")
    db.commit()
    db.refresh(file)
    return file


def remove_file(db: Session, report_id, file_id, actor: str = "System") -> None:
    report = db.query(InspectionReport).filter(InspectionReport.id == report_id).first()
    file = db.query(InspectionReportFile).filter(InspectionReportFile.id == file_id, InspectionReportFile.report_id == report_id).first()
    if report is None or file is None:
        raise LookupError("File not found")
    ensure_editable(report, actor)
    audit.record(db, actor, "report.file_remove", str(report.id), f"{report.equipment_tag} | {report.report_no} | {file.filename}")
    db.delete(file)
    db.commit()


def get_file(db: Session, report_id, file_id) -> Optional[InspectionReportFile]:
    return db.query(InspectionReportFile).filter(InspectionReportFile.id == file_id, InspectionReportFile.report_id == report_id).first()
