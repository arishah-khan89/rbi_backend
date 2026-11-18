import mimetypes
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import Principal, authorize
from app.core.approval import NotOwnerError, RecordLockedError
from app.core.config import settings
from app.core.database import get_db
from app.core.uploads import DOCUMENT_TYPES, MAX_DOCUMENT_BYTES, read_upload, safe_filename
from app.crud import inspection_report as crud
from app.crud import report_workflow as workflow
from app.models.audit import AuditLog
from app.schemas.auth import AuditOut
from app.schemas.inspection_report import (
    RecipientOut, ReportCreate, ReportDecision, ReportFileOut, ReportListResponse, ReportOut, ReportReason,
    ReportReturn, ReportSubmit, ReportSummary, ReportUpdate)

router = APIRouter()

ATTACHMENT_TYPES = DOCUMENT_TYPES + (".xlsx", ".xls")          # reports, photos, calculations


def _run(call, *args):
    """Run one step and turn its errors into HTTP answers."""
    try:
        return call(*args)
    except RecordLockedError as err:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(err))
    except (NotOwnerError, PermissionError) as err:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(err))
    except LookupError as err:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(err))
    except ValueError as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(err))


def _needs_sign_in():
    if not settings.AUTH_REQUIRED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Approvals need sign-in to be switched on.")


def _found(report):
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Inspection report not found")
    return report


# ---------------------------------------------------------------- lists (fixed paths before /{report_id})
@router.get("", response_model=ReportListResponse)
def list_reports(
    equipment_id: Optional[uuid.UUID] = Query(None, description="Only the reports of this equipment"),
    status_filter: Optional[str] = Query(None, alias="status", description="draft, pending, returned or approved"),
    q: Optional[str] = Query(None, description="Search number, title, reason, summary or equipment"),
    mine: bool = Query(False, description="Only the reports I wrote or sent"),
    assigned_to_me: bool = Query(False, description="Only the reports waiting for my decision"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    principal: Principal = Depends(authorize),
):
    me = principal.username if settings.AUTH_REQUIRED else None
    return crud.list_reports(db, equipment_id, status_filter, q, mine=me if mine else None,
                             assigned_to=me if assigned_to_me else None, page=page, page_size=page_size)


@router.get("/summary", response_model=ReportSummary)
def summary(db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    if not settings.AUTH_REQUIRED:
        return ReportSummary()
    return ReportSummary(**crud.summary(db, principal.username, principal.role == "admin"))


@router.get("/recipients", response_model=list[RecipientOut])
def recipients(db: Session = Depends(get_db)):
    """Who can be chosen under "Send to" (names only)."""
    return crud.recipients_choice(db)


# ---------------------------------------------------------------- one report
@router.post("", response_model=ReportOut, status_code=status.HTTP_201_CREATED)
def create_report(data: ReportCreate, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    return _run(crud.create_report, db, data, principal.username)


@router.get("/{report_id}", response_model=ReportOut)
def get_report(report_id: uuid.UUID, db: Session = Depends(get_db)):
    return _found(crud.get_report(db, report_id))


@router.put("/{report_id}", response_model=ReportOut)
def update_report(report_id: uuid.UUID, data: ReportUpdate, db: Session = Depends(get_db),
                  principal: Principal = Depends(authorize)):
    return _found(_run(crud.update_report, db, report_id, data, principal.username))


@router.delete("/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_report(report_id: uuid.UUID, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    if not _run(crud.delete_report, db, report_id, principal.username):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Inspection report not found")


@router.get("/{report_id}/history", response_model=list[AuditOut])
def report_history(report_id: uuid.UUID, db: Session = Depends(get_db)):
    """Who did what with this report (created, changed, files, sent, approved, returned ...), oldest first."""
    _found(crud.get_report(db, report_id))
    return db.query(AuditLog).filter(AuditLog.target == str(report_id), AuditLog.action.like("report.%")) \
        .order_by(AuditLog.at.asc()).all()


# ---------------------------------------------------------------- files (only for signed-in people, never a public folder)
@router.post("/{report_id}/files", response_model=ReportFileOut, status_code=status.HTTP_201_CREATED)
async def upload_file(report_id: uuid.UUID, file: UploadFile = File(...), db: Session = Depends(get_db),
                      principal: Principal = Depends(authorize)):
    content, _ext = await read_upload(file, ATTACHMENT_TYPES, MAX_DOCUMENT_BYTES)      # size, type and real content are checked
    name = safe_filename(file.filename)
    return _run(crud.add_file, db, report_id, name, content, mimetypes.guess_type(name)[0], principal.username)


@router.get("/{report_id}/files/{file_id}")
def download_file(report_id: uuid.UUID, file_id: uuid.UUID, db: Session = Depends(get_db)):
    file = crud.get_file(db, report_id, file_id)
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="File not found")
    return Response(content=file.content, media_type=file.content_type or "application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="{file.filename}"',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
    })


@router.delete("/{report_id}/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_file(report_id: uuid.UUID, file_id: uuid.UUID, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    _run(crud.remove_file, db, report_id, file_id, principal.username)


# ---------------------------------------------------------------- approval
@router.post("/{report_id}/submit", response_model=ReportOut)
def submit(report_id: uuid.UUID, data: ReportSubmit, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    _needs_sign_in()
    return _run(workflow.submit, db, report_id, principal.username, data.approver, data.comment)


@router.post("/{report_id}/withdraw", response_model=ReportOut)
def withdraw(report_id: uuid.UUID, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    _needs_sign_in()
    return _run(workflow.withdraw, db, report_id, principal.username, principal.role)


@router.post("/{report_id}/approve", response_model=ReportOut)
def approve(report_id: uuid.UUID, data: ReportDecision, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    _needs_sign_in()
    return _run(workflow.approve, db, report_id, principal.username, data.comment)


@router.post("/{report_id}/return", response_model=ReportOut)
def return_report(report_id: uuid.UUID, data: ReportReturn, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    _needs_sign_in()
    return _run(workflow.return_to_sender, db, report_id, principal.username, data.comment)


@router.post("/{report_id}/reject", response_model=ReportOut)
def reject(report_id: uuid.UUID, data: ReportReason, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    _needs_sign_in()
    return _run(workflow.reject, db, report_id, principal.username, data.reason)


@router.post("/{report_id}/reopen", response_model=ReportOut)
def reopen(report_id: uuid.UUID, data: ReportReason, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    _needs_sign_in()
    return _run(workflow.reopen, db, report_id, principal.username, principal.role, data.reason)
