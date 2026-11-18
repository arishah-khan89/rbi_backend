r"""Approval of inspection reports (maker-checker), the same rules as for inspections:

  draft --send (to ONE admin)--> pending --approve--> approved (locked)
                                    |  \--return (with a comment)--> returned --(change, send again)--> pending
                                    \--withdraw (the sender)--> draft
                                    \--reject (with a reason)--> rejected (final: closed, nobody changes it)
  approved --reopen (the admin who approved it, or a super admin; a reason is required)--> returned
"returned" is what Meridium calls "Re-referred": sent back to the person to correct and send again.

Every step is enforced here on the server, written to the audit trail, and tells the people it concerns.
Approving a report does NOT change the dates of its tasks: the person writes them.
Errors: ValueError -> 400, PermissionError -> 403, LookupError -> 404 (mapped by the endpoints).
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.core import audit
from app.core.approval import APPROVED, DRAFT, EDITABLE, PENDING, RETURNED
from app.crud.inspection_report import REJECTED
from app.crud import notification as notifications
from app.crud.approval import check_reviewer
from app.crud.inspection_report import _recipient_names, get_report, ownership_problem
from app.models.inspection_report import InspectionReport, InspectionReportTask


def _now():
    return datetime.now(timezone.utc)


def _report(db: Session, report_id) -> InspectionReport:
    report = db.query(InspectionReport).filter(InspectionReport.id == report_id).first()
    if report is None:
        raise LookupError("Inspection report not found")
    return report


def _what(r: InspectionReport) -> str:
    return f"report {r.report_no} ({r.equipment_tag})"


def _same(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a) and bool(b) and a.lower() == b.lower()


def _decide_guard(r: InspectionReport, actor: str, what: str) -> None:
    if r.approval_status != PENDING:
        raise ValueError(f"Only a report that is waiting for approval can be {what}.")
    if not _same(r.assigned_reviewer, actor):
        raise PermissionError(f"Only {r.assigned_reviewer} can decide on this report; it was sent to that admin.")
    if _same(r.submitted_by, actor):
        raise PermissionError("You cannot decide on a report that you sent yourself.")


def submit(db: Session, report_id, actor: str, approver: str, comment: Optional[str]) -> InspectionReport:
    r = _report(db, report_id)
    if r.approval_status not in EDITABLE:
        raise ValueError("Only a draft or a returned report can be sent for approval.")
    problem = ownership_problem(r, actor)
    if problem:
        raise PermissionError(problem)
    if db.query(InspectionReportTask).filter(InspectionReportTask.report_id == r.id).count() == 0:
        raise ValueError("A report must cover at least one inspection task before it is sent for approval.")
    reviewer = check_reviewer(db, approver, actor)
    note = (comment or "").strip() or None

    r.approval_status = PENDING
    r.submitted_by = actor
    r.submitted_at = _now()
    r.assigned_reviewer = reviewer.username
    r.reviewed_by = None
    r.reviewed_at = None
    r.review_comment = None
    r.submit_comment = note
    audit.record(db, actor, "report.submit", str(r.id), f"{r.equipment_tag} | {r.report_no} -> {reviewer.username}" + (f" | {note}" if note else ""))
    notifications.notify(db, reviewer.username, "report_assigned", f"{actor} sent you {_what(r)} for approval", note, r, actor)
    for name in _recipient_names(db, r.id):                                  # "Send to": told, but only the approver decides
        if name != reviewer.username:
            notifications.notify(db, name, "report_shared", f"{actor} sent you {_what(r)} (for your information)", note, r, actor)
    db.commit()
    return get_report(db, r.id)


def approve(db: Session, report_id, actor: str, comment: Optional[str]) -> InspectionReport:
    r = _report(db, report_id)
    _decide_guard(r, actor, "approved")
    note = (comment or "").strip() or None
    r.approval_status = APPROVED
    r.reviewed_by = actor
    r.reviewed_at = _now()
    r.review_comment = note
    audit.record(db, actor, "report.approve", str(r.id), f"{r.equipment_tag} | {r.report_no}" + (f" | {note}" if note else ""))
    notifications.notify(db, r.submitted_by, "report_approved", f"{actor} approved {_what(r)}", note, r, actor)
    for name in _recipient_names(db, r.id):
        if name != (r.submitted_by or "").lower():
            notifications.notify(db, name, "report_approved", f"{actor} approved {_what(r)}", note, r, actor)
    db.commit()
    return get_report(db, r.id)


def return_to_sender(db: Session, report_id, actor: str, comment: str) -> InspectionReport:
    r = _report(db, report_id)
    _decide_guard(r, actor, "returned")
    r.approval_status = RETURNED
    r.reviewed_by = actor
    r.reviewed_at = _now()
    r.review_comment = comment
    audit.record(db, actor, "report.return", str(r.id), f"{r.equipment_tag} | {r.report_no} | {comment}")
    notifications.notify(db, r.submitted_by, "report_returned", f"{actor} re-referred {_what(r)} to you", comment, r, actor)
    db.commit()
    return get_report(db, r.id)


def reject(db: Session, report_id, actor: str, reason: str) -> InspectionReport:
    """A final "no": the report is closed and cannot be sent again. The reason is required and goes to the sender."""
    r = _report(db, report_id)
    _decide_guard(r, actor, "rejected")
    r.approval_status = REJECTED
    r.reviewed_by = actor
    r.reviewed_at = _now()
    r.review_comment = reason
    audit.record(db, actor, "report.reject", str(r.id), f"{r.equipment_tag} | {r.report_no} | {reason}")
    notifications.notify(db, r.submitted_by, "report_rejected", f"{actor} rejected {_what(r)}", reason, r, actor)
    for name in _recipient_names(db, r.id):
        if name != (r.submitted_by or "").lower():
            notifications.notify(db, name, "report_rejected", f"{actor} rejected {_what(r)}", reason, r, actor)
    db.commit()
    return get_report(db, r.id)


def withdraw(db: Session, report_id, actor: str, role: str) -> InspectionReport:
    r = _report(db, report_id)
    if r.approval_status != PENDING:
        raise ValueError("Only a report that is waiting for approval can be withdrawn.")
    if not (_same(r.submitted_by, actor) or role == "super_admin"):
        raise PermissionError("Only the person who sent it (or a super admin) can withdraw it.")
    reviewer = r.assigned_reviewer
    r.approval_status = DRAFT
    r.assigned_reviewer = None
    audit.record(db, actor, "report.withdraw", str(r.id), f"{r.equipment_tag} | {r.report_no}")
    notifications.notify(db, reviewer, "report_withdrawn", f"{actor} withdrew {_what(r)}: it no longer needs your decision", None, r, actor)
    db.commit()
    return get_report(db, r.id)


def reopen(db: Session, report_id, actor: str, role: str, reason: str) -> InspectionReport:
    r = _report(db, report_id)
    if r.approval_status != APPROVED:
        raise ValueError("Only an approved report can be reopened.")
    if not (_same(r.reviewed_by, actor) or role == "super_admin"):
        raise PermissionError(f"Only {r.reviewed_by} (who approved it) or a super admin can reopen this report.")
    r.approval_status = RETURNED
    r.review_comment = f"Reopened: {reason}"
    r.reviewed_by = actor
    r.reviewed_at = _now()
    audit.record(db, actor, "report.reopen", str(r.id), f"{r.equipment_tag} | {r.report_no} | {reason}")
    notifications.notify(db, r.submitted_by, "report_reopened", f"{actor} reopened {_what(r)}: it is back with you for correction",
                         reason, r, actor)
    db.commit()
    return get_report(db, r.id)
