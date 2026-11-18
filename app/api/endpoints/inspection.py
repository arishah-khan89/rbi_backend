from datetime import datetime, time as dtime
from fastapi import APIRouter, Depends, Form, HTTPException, Query, status, File, UploadFile
from sqlalchemy.orm import Session
from typing import Optional
import uuid

from app.core.approval import NotOwnerError, RecordLockedError
from app.core.database import get_db
from app.core.config import settings
from app.api.deps import Principal, authorize
from app.schemas.inspection import (
    ApprovalSummary,
    DecidedItem,
    DecisionRequest,
    MyTasks,
    ReasonRequest,
    ReturnRequest,
    SubmitRequest,
    InspectionRecordCreate,
    InspectionRecordResponse,
    InspectionRecordListResponse,
    InspectionStats,
)
from app.crud import approval as crud_approval
from app.crud import notification as notifications
from app.crud import inspection as crud_inspection
from app.schemas.auth import AuditOut
from app.crud.inspection_import import import_inspections_from_excel
from app.core.uploads import read_upload, SPREADSHEET_TYPES, MAX_SPREADSHEET_BYTES

router = APIRouter()


def _server_error(prefix: str, err: Exception) -> HTTPException:
    """500 response. RuntimeError messages are written for the user and are shown; the text of any other
    unexpected error is shown only in development (it can contain internal details)."""
    if isinstance(err, RuntimeError):
        detail = str(err)
    elif settings.APP_ENV == "development":
        detail = f"{prefix}: {err}"
    else:
        detail = f"{prefix}. Please try again or contact support."
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail)


@router.get("", response_model=InspectionRecordListResponse)
def get_inspection_list(
    equipment_tag: Optional[str] = Query(None, description="Filter by Equipment Tag"),
    equipment_id: Optional[uuid.UUID] = Query(None, description="Only the inspections of this equipment"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db)
):
    items, total, total_pages = crud_inspection.get_inspection_list(
        db=db,
        equipment_tag=equipment_tag,
        page=page,
        page_size=page_size,
        equipment_id=equipment_id,
    )
    return InspectionRecordListResponse(
        data=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages
    )


@router.get("/stats", response_model=InspectionStats)
def get_inspection_stats(db: Session = Depends(get_db)):
    return crud_inspection.get_inspection_stats(db=db)


@router.post("", response_model=InspectionRecordResponse, status_code=status.HTTP_201_CREATED)
def create_inspection_record(
    data: InspectionRecordCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(authorize),
):
    data.created_by = principal.username
    if settings.AUTH_REQUIRED:
        data.inspector_name = principal.full_name or principal.username      # the person who enters it is the inspector
    try:
        return crud_inspection.create_inspection_record(db=db, data=data)
    except NotOwnerError as err:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(err))
    except ValueError as err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err))
    except Exception as err:
        raise _server_error("Failed to save inspection record", err)


@router.get("/sample-template")
def download_sample_template():
    """The inspection import template: instructions, an empty 'Inspection Data' sheet with checks, and an example sheet."""
    import io
    from fastapi.responses import StreamingResponse
    from app.crud.inspection_template import build_template_workbook

    return StreamingResponse(
        io.BytesIO(build_template_workbook()),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="Inspection_Import_Template.xlsx"'},
    )


# ---------------------------------------------------------------- approval workflow
def _needs_sign_in():
    if not settings.AUTH_REQUIRED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Approvals need sign-in to be switched on.")


def _workflow(call, *args):
    """Run one workflow step and turn its errors into HTTP answers."""
    _needs_sign_in()
    try:
        return call(*args)
    except LookupError as err:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(err))
    except PermissionError as err:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(err))
    except ValueError as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(err))


def _with_zones(db: Session, rec):
    rec.tml_readings = crud_inspection.populate_record_tml_readings(db, rec)
    return rec


@router.get("/approvals", response_model=list[InspectionRecordResponse])
def list_pending_approvals(mine: bool = False, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    """Inspections waiting for approval (every admin sees all of them; only the assigned one can decide)."""
    return [_with_zones(db, r) for r in crud_approval.pending(db, principal.username if mine else None)]


@router.get("/my-tasks", response_model=MyTasks)
def my_tasks(db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    """What I have to do or am waiting for: my drafts, what came back to me, what I sent, what is waiting for my decision."""
    if not settings.AUTH_REQUIRED:
        return MyTasks()
    return MyTasks(**crud_approval.my_tasks(db, principal.username, principal.role in ("admin", "super_admin")))


@router.get("/returned-to-me", response_model=list[InspectionRecordResponse])
def list_returned_to_me(db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    """The inspections I sent that an admin returned, with the admin's comment (any signed-in person)."""
    if not settings.AUTH_REQUIRED:
        return []
    return [_with_zones(db, r) for r in crud_approval.returned_to(db, principal.username)]


@router.get("/approvals/decided", response_model=list[DecidedItem])
def list_decided(
    decision: Optional[str] = Query(None, description="approved or returned"),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD"),
    db: Session = Depends(get_db),
    principal: Principal = Depends(authorize),
):
    """What I approved or returned (a super admin sees the decisions of every admin)."""
    if decision not in (None, "", "approved", "returned"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="decision must be 'approved' or 'returned'.")
    try:
        start = datetime.strptime(date_from, "%Y-%m-%d") if date_from else None
        end = datetime.combine(datetime.strptime(date_to, "%Y-%m-%d").date(), dtime.max) if date_to else None
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Dates must be written as YYYY-MM-DD.")
    return crud_approval.decided(db, principal.username, everyone=principal.role == "super_admin",
                                 decision=decision or None, date_from=start, date_to=end)


@router.get("/approvals/summary", response_model=ApprovalSummary)
def approval_summary(db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    if not settings.AUTH_REQUIRED:
        return ApprovalSummary()
    return ApprovalSummary(**crud_approval.summary(db, principal.username))


@router.post("/{record_id}/submit", response_model=InspectionRecordResponse)
def submit_for_approval(record_id: uuid.UUID, data: SubmitRequest, db: Session = Depends(get_db),
                        principal: Principal = Depends(authorize)):
    rec = _workflow(crud_approval.submit, db, record_id, principal.username, data.reviewer, data.comment)
    return _with_zones(db, rec)


@router.post("/{record_id}/withdraw", response_model=InspectionRecordResponse)
def withdraw_from_approval(record_id: uuid.UUID, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    return _with_zones(db, _workflow(crud_approval.withdraw, db, record_id, principal.username, principal.role))


@router.post("/{record_id}/approve", response_model=InspectionRecordResponse)
def approve_inspection(record_id: uuid.UUID, data: DecisionRequest, db: Session = Depends(get_db),
                       principal: Principal = Depends(authorize)):
    return _with_zones(db, _workflow(crud_approval.approve, db, record_id, principal.username, data.comment))


@router.post("/{record_id}/return", response_model=InspectionRecordResponse)
def return_inspection(record_id: uuid.UUID, data: ReturnRequest, db: Session = Depends(get_db),
                      principal: Principal = Depends(authorize)):
    return _with_zones(db, _workflow(crud_approval.return_to_sender, db, record_id, principal.username, data.comment))


@router.post("/{record_id}/reopen", response_model=InspectionRecordResponse)
def reopen_inspection(record_id: uuid.UUID, data: ReasonRequest, db: Session = Depends(get_db),
                      principal: Principal = Depends(authorize)):
    return _with_zones(db, _workflow(crud_approval.reopen, db, record_id, principal.username, principal.role, data.reason))


@router.get("/{record_id}/history", response_model=list[AuditOut])
def inspection_history(record_id: uuid.UUID, db: Session = Depends(get_db)):
    try:
        return crud_approval.history(db, record_id)
    except LookupError as err:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(err))


@router.get("/{record_id}", response_model=InspectionRecordResponse)
def get_inspection_record(
    record_id: uuid.UUID,
    db: Session = Depends(get_db)
):
    record = crud_inspection.get_inspection_record(db=db, record_id=record_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inspection record not found")
    return record


@router.put("/{record_id}", response_model=InspectionRecordResponse)
def update_inspection_record(
    record_id: uuid.UUID,
    data: InspectionRecordCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(authorize),
):
    data.created_by = principal.username
    if settings.AUTH_REQUIRED:
        existing = crud_inspection.get_inspection_record(db=db, record_id=record_id)
        if existing is not None:
            data.inspector_name = existing.inspector_name       # whoever entered it stays the inspector; the edit is in the history
    try:
        return crud_inspection.update_inspection_record(db=db, record_id=record_id, data=data)
    except NotOwnerError as err:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(err))
    except ValueError as err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err))
    except Exception as err:
        raise _server_error("Failed to update inspection record", err)


@router.delete("/{record_id}", status_code=status.HTTP_200_OK)
def delete_inspection_record(
    record_id: uuid.UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(authorize),
):
    try:
        crud_inspection.delete_inspection_record(db=db, record_id=record_id, actor=principal.username)
        return {"status": "success", "message": "Inspection record deleted successfully"}
    except RecordLockedError as err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err))
    except ValueError as err:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(err))
    except Exception as err:
        raise _server_error("Failed to delete inspection record", err)


@router.post("/import-excel")
async def import_inspection_excel(
    file: UploadFile = File(...),
    reviewer: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    principal: Principal = Depends(authorize),
):
    """Bulk import inspection thickness records from Excel (.xlsx) or CSV file.

    With sign-in on, ONE reviewer (an admin) is chosen for the whole file: every inspection the import creates or
    changes is sent to that admin and starts as Pending approval (locked until they decide)."""
    contents, _ext = await read_upload(file, SPREADSHEET_TYPES, MAX_SPREADSHEET_BYTES)

    reviewer_user = None
    if settings.AUTH_REQUIRED:
        try:
            reviewer_user = crud_approval.check_reviewer(db, reviewer, principal.username)
        except ValueError as err:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"Choose the admin who will review this import. {err}")

    collected: list = []
    token = crud_inspection.IMPORT_COLLECTOR.set(collected)
    actor_token = crud_inspection.IMPORT_ACTOR.set(principal.username)
    try:
        result = import_inspections_from_excel(
            db=db,
            file_contents=contents,
            filename=file.filename
        )
    except ValueError as err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err))
    except Exception as err:
        raise _server_error("Failed to import file", err)
    finally:
        crud_inspection.IMPORT_COLLECTOR.reset(token)
        crud_inspection.IMPORT_ACTOR.reset(actor_token)

    if reviewer_user is not None:
        sent = 0
        for record_id in dict.fromkeys(collected):          # each inspection once, in the order it was saved
            try:
                crud_approval.submit(db, record_id, principal.username, reviewer_user.username, f"Imported from {file.filename}",
                                     notify=False)
                sent += 1
            except (ValueError, LookupError) as err:
                result.setdefault("errors", []).append(f"An imported inspection could not be sent for approval: {err}")
        if sent:                                          # one message for the whole file, not one per inspection
            notifications.notify(db, reviewer_user.username, "assigned",
                                 f"{principal.username} sent you {sent} imported inspection(s) from {file.filename} for approval",
                                 None, None, principal.username)
            db.commit()
        result["sent_for_approval"] = sent
        result["reviewer"] = reviewer_user.username
    return result
