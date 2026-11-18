"""Approval workflow of inspection records (maker-checker).

  draft --submit(to ONE admin)--> pending --approve--> approved (locked)
                                     |  \--return (with a comment)--> returned --(edit, submit again)--> pending
                                     \--withdraw (sender)--> draft
  approved --reopen (the admin who approved it, or a super admin; a reason is required)--> returned

Every rule is enforced here, on the server. Every step is written to the audit trail.
Errors: ValueError -> 400, PermissionError -> 403, LookupError -> 404 (mapped by the endpoints).
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.core import audit
from app.crud import inspection as crud_inspection
from app.crud import notification as notifications
from app.core.approval import APPROVED, DRAFT, PENDING, RETURNED, REVIEWER_ROLES, ownership_problem
from app.models.audit import AuditLog
from app.models.inspection import InspectionRecord
from app.models.user import User


def _record(db: Session, record_id) -> InspectionRecord:
    rec = db.query(InspectionRecord).filter(InspectionRecord.id == record_id).first()
    if rec is None:
        raise LookupError("Inspection record not found")
    return rec


def _now():
    return datetime.now(timezone.utc)


def _label(rec: InspectionRecord) -> str:
    return f"{rec.equipment_tag} {rec.inspection_date:%Y-%m-%d}"


def _same(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a) and bool(b) and a.lower() == b.lower()


def reviewers(db: Session):
    """Active admins: the people a record can be sent to."""
    return db.query(User).filter(User.is_active.is_(True), User.role.in_(REVIEWER_ROLES)).order_by(User.username).all()


def check_reviewer(db: Session, reviewer_username: Optional[str], actor: str) -> User:
    """The chosen reviewer must be an active admin other than the sender."""
    reviewer = db.query(User).filter(User.username == (reviewer_username or "").strip().lower()).first()
    if reviewer is None or not reviewer.is_active or reviewer.role not in REVIEWER_ROLES:
        raise ValueError("Choose an active admin as the reviewer.")
    if _same(reviewer.username, actor):
        raise ValueError("You cannot send your own inspection to yourself. Choose another admin.")
    return reviewer


def _unlocked(db: Session, rec: InspectionRecord) -> None:
    """The inspection is open again (returned, withdrawn or reopened): it follows the equipment data again, so its
    numbers are recalculated with today's t_min."""
    from app.crud.cml import recalculate_equipment_cmls
    recalculate_equipment_cmls(db, rec.equipment_id)
    db.refresh(rec)


def _what(rec: InspectionRecord) -> str:
    return f"the inspection of {rec.equipment_tag} ({rec.inspection_date:%d-%b-%Y})"


def submit(db: Session, record_id, actor: str, reviewer_username: str, comment: Optional[str],
           notify: bool = True) -> InspectionRecord:
    """`notify=False` is for an import, which sends the reviewer one message for the whole file instead of one per inspection."""
    rec = _record(db, record_id)
    if rec.approval_status not in (DRAFT, RETURNED):
        raise ValueError("Only a draft or a returned inspection can be sent for approval.")
    problem = ownership_problem(rec, actor)
    if problem:
        raise PermissionError(problem)
    reviewer = check_reviewer(db, reviewer_username, actor)
    rec.approval_status = PENDING
    rec.submitted_by = actor
    rec.submitted_at = _now()
    rec.assigned_reviewer = reviewer.username
    rec.reviewed_by = None
    rec.reviewed_at = None
    rec.review_comment = None
    rec.submit_comment = (comment or '').strip() or None
    rec.locked_snapshot = crud_inspection.take_snapshot(db, rec)       # the t_min it is judged with stays with it while locked
    audit.record(db, actor, "inspection.submit", str(rec.id), f"{_label(rec)} -> {reviewer.username}" + (f" | {comment}" if comment else ""))
    if notify:
        notifications.notify(db, reviewer.username, "assigned", f"{actor} sent you {_what(rec)} for approval",
                             (comment or '').strip() or None, rec, actor)
    db.commit()
    return rec


def _decide_guard(rec: InspectionRecord, actor: str, what: str):
    if rec.approval_status != PENDING:
        raise ValueError(f"Only an inspection that is waiting for approval can be {what}.")
    if not _same(rec.assigned_reviewer, actor):
        raise PermissionError(f"Only {rec.assigned_reviewer} can decide on this inspection; it was sent to that admin.")
    if _same(rec.submitted_by, actor):
        raise PermissionError("You cannot decide on an inspection that you sent yourself.")


def approve(db: Session, record_id, actor: str, comment: Optional[str]) -> InspectionRecord:
    rec = _record(db, record_id)
    _decide_guard(rec, actor, "approved")
    rec.approval_status = APPROVED
    rec.reviewed_by = actor
    rec.reviewed_at = _now()
    rec.review_comment = comment or None
    audit.record(db, actor, "inspection.approve", str(rec.id), f"{_label(rec)}" + (f" | {comment}" if comment else ""))
    notifications.notify(db, rec.submitted_by, "approved", f"{actor} approved {_what(rec)}", comment or None, rec, actor)
    db.commit()
    return rec


def return_to_sender(db: Session, record_id, actor: str, comment: str) -> InspectionRecord:
    rec = _record(db, record_id)
    _decide_guard(rec, actor, "returned")
    rec.approval_status = RETURNED
    rec.reviewed_by = actor
    rec.reviewed_at = _now()
    rec.review_comment = comment
    rec.locked_snapshot = None
    audit.record(db, actor, "inspection.return", str(rec.id), f"{_label(rec)} | {comment}")
    notifications.notify(db, rec.submitted_by, "returned", f"{actor} returned {_what(rec)} to you", comment, rec, actor)
    db.commit()
    _unlocked(db, rec)
    return rec


def withdraw(db: Session, record_id, actor: str, role: str) -> InspectionRecord:
    rec = _record(db, record_id)
    if rec.approval_status != PENDING:
        raise ValueError("Only an inspection that is waiting for approval can be withdrawn.")
    if not (_same(rec.submitted_by, actor) or role == "super_admin"):
        raise PermissionError("Only the person who sent it (or a super admin) can withdraw it.")
    reviewer = rec.assigned_reviewer
    rec.approval_status = DRAFT
    rec.assigned_reviewer = None
    rec.locked_snapshot = None
    audit.record(db, actor, "inspection.withdraw", str(rec.id), _label(rec))
    notifications.notify(db, reviewer, "withdrawn", f"{actor} withdrew {_what(rec)}: it no longer needs your decision",
                         None, rec, actor)
    db.commit()
    _unlocked(db, rec)
    return rec


def reopen(db: Session, record_id, actor: str, role: str, reason: str) -> InspectionRecord:
    rec = _record(db, record_id)
    if rec.approval_status != APPROVED:
        raise ValueError("Only an approved inspection can be reopened.")
    if not (_same(rec.reviewed_by, actor) or role == "super_admin"):
        raise PermissionError(f"Only {rec.reviewed_by} (who approved it) or a super admin can reopen this inspection.")
    rec.approval_status = RETURNED
    rec.locked_snapshot = None
    rec.review_comment = f"Reopened: {reason}"
    rec.reviewed_by = actor
    rec.reviewed_at = _now()
    audit.record(db, actor, "inspection.reopen", str(rec.id), f"{_label(rec)} | {reason}")
    notifications.notify(db, rec.submitted_by, "reopened", f"{actor} reopened {_what(rec)}: it is back with you for correction",
                         reason, rec, actor)
    db.commit()
    _unlocked(db, rec)
    return rec


def pending(db: Session, only_for: Optional[str] = None):
    q = db.query(InspectionRecord).filter(InspectionRecord.approval_status == PENDING)
    if only_for:
        q = q.filter(InspectionRecord.assigned_reviewer == only_for.lower())
    return q.order_by(InspectionRecord.submitted_at.asc()).all()


def returned_to(db: Session, username: str):
    """Inspections this person sent and an admin sent back (they are waiting to be corrected and sent again)."""
    return db.query(InspectionRecord).filter(
        InspectionRecord.approval_status == RETURNED, InspectionRecord.submitted_by.ilike(username)
    ).order_by(InspectionRecord.reviewed_at.desc()).all()


TASK_LIST_LIMIT = 25


def my_tasks(db: Session, username: str, is_reviewer: bool) -> dict:
    """"My tasks": the inspections that wait for something from this person, and the ones they wait for."""
    me = username.lower()
    base = db.query(InspectionRecord)

    def take(query, order):
        return query.order_by(order).limit(TASK_LIST_LIMIT).all(), query.count()

    mine_sent = InspectionRecord.submitted_by.ilike(username)
    returned, returned_total = take(base.filter(InspectionRecord.approval_status == RETURNED, mine_sent),
                                    InspectionRecord.reviewed_at.desc())
    drafts, drafts_total = take(
        base.filter(InspectionRecord.approval_status == DRAFT,
                    InspectionRecord.submitted_by.ilike(username) | (
                        InspectionRecord.submitted_by.is_(None) & InspectionRecord.created_by.ilike(username))),
        InspectionRecord.updated_at.desc())
    waiting, waiting_total = take(base.filter(InspectionRecord.approval_status == PENDING, mine_sent),
                                  InspectionRecord.submitted_at.desc())
    approved = base.filter(InspectionRecord.approval_status == APPROVED, mine_sent) \
        .order_by(InspectionRecord.reviewed_at.desc()).limit(5).all()
    awaiting, awaiting_total = ([], 0)
    if is_reviewer:
        awaiting, awaiting_total = take(
            base.filter(InspectionRecord.approval_status == PENDING, InspectionRecord.assigned_reviewer == me),
            InspectionRecord.submitted_at.asc())
    return {
        "awaiting_my_decision": awaiting, "returned": returned, "drafts": drafts,
        "waiting_for_approval": waiting, "recently_approved": approved,
        "awaiting_my_decision_total": awaiting_total, "returned_total": returned_total,
        "drafts_total": drafts_total, "waiting_for_approval_total": waiting_total,
        "pending_in_system": base.filter(InspectionRecord.approval_status == PENDING).count(),
    }


def summary(db: Session, username: str) -> dict:
    base = db.query(InspectionRecord)
    return {
        "assigned_to_me": base.filter(InspectionRecord.approval_status == PENDING, InspectionRecord.assigned_reviewer == username.lower()).count(),
        "returned_to_me": base.filter(InspectionRecord.approval_status == RETURNED, InspectionRecord.submitted_by == username).count(),
        "pending_total": base.filter(InspectionRecord.approval_status == PENDING).count(),
    }


def decided(db: Session, actor: str, everyone: bool = False, decision: Optional[str] = None,
            date_from: Optional[datetime] = None, date_to: Optional[datetime] = None, limit: int = 500) -> list[dict]:
    """The approvals and returns an admin took (newest first). They are read from the audit trail, so the list also
    keeps the decisions on inspections that were reopened or corrected afterwards: `current_status` says where each stands.
    `everyone` (the super admin's oversight) lists the decisions of all admins."""
    q = db.query(AuditLog).filter(AuditLog.action.in_(("inspection.approve", "inspection.return")))
    if not everyone:
        q = q.filter(AuditLog.actor.ilike(actor))
    if decision in ("approved", "returned"):
        q = q.filter(AuditLog.action == ("inspection.approve" if decision == "approved" else "inspection.return"))
    if date_from:
        q = q.filter(AuditLog.at >= date_from)
    if date_to:
        q = q.filter(AuditLog.at <= date_to)
    lines = q.order_by(AuditLog.at.desc()).limit(limit).all()

    ids = []
    for line in lines:
        try:
            ids.append(uuid.UUID(str(line.target)))
        except (ValueError, AttributeError):
            pass
    records = {r.id: r for r in db.query(InspectionRecord).filter(InspectionRecord.id.in_(ids)).all()} if ids else {}

    out = []
    for line in lines:
        try:
            rec = records.get(uuid.UUID(str(line.target)))
        except (ValueError, AttributeError):
            rec = None
        detail = line.detail or ""
        comment = detail.split(" | ", 1)[1] if " | " in detail else None
        out.append({
            "audit_id": line.id,
            "decided_at": line.at,
            "decision": "approved" if line.action == "inspection.approve" else "returned",
            "decided_by": line.actor,
            "comment": comment,
            "record_id": rec.id if rec else None,
            "equipment_tag": rec.equipment_tag if rec else (detail.split(" | ", 1)[0].rsplit(" ", 1)[0] or None),
            "inspection_date": rec.inspection_date if rec else None,
            "inspector_name": rec.inspector_name if rec else None,
            "submitted_by": rec.submitted_by if rec else None,
            "reviewed_by": rec.reviewed_by if rec else None,
            "current_status": rec.approval_status if rec else None,
        })
    return out


def history(db: Session, record_id):
    _record(db, record_id)
    return db.query(AuditLog).filter(AuditLog.target == str(record_id), AuditLog.action.like("inspection.%")) \
        .order_by(AuditLog.at.asc()).all()
