import mimetypes
import uuid
from fastapi import APIRouter, Depends, HTTPException, Query, Response, UploadFile, File
from sqlalchemy.orm import Session
from typing import Optional
from app.core.config import settings
from app.core.database import get_db
from app.models.audit import AuditLog
from app.models.equipment import EquipmentDocument
from app.schemas.auth import AuditOut
from app.api.deps import Principal, authorize
from app.schemas.equipment import (
    EquipmentCreate, EquipmentUpdate,
    EquipmentResponse, EquipmentListResponse, EquipmentStats, InspectionDueItem
)
from app.crud import equipment as crud
from app.core.uploads import read_upload, safe_filename, DOCUMENT_TYPES, MAX_DOCUMENT_BYTES

router = APIRouter(prefix="/equipment", tags=["Equipment"])

DOCUMENT_URL_PREFIX = "/api/v1/equipment/documents/"


@router.post("/upload-document")
async def upload_document(file: UploadFile = File(...), db: Session = Depends(get_db),
                          principal: Principal = Depends(authorize)):
    # size limit + real file type check (not just the extension); the file is kept in the database, not in a folder
    content, _ext = await read_upload(file, DOCUMENT_TYPES, MAX_DOCUMENT_BYTES)
    name = safe_filename(file.filename)
    doc = EquipmentDocument(filename=name, content_type=mimetypes.guess_type(name)[0], size_bytes=len(content),
                            content=content, uploaded_by=principal.username)
    db.add(doc)
    db.commit()
    return {"document_name": file.filename, "document_url": f"{DOCUMENT_URL_PREFIX}{doc.id}"}


@router.get("/documents/{document_id}")
def download_document(document_id: uuid.UUID, db: Session = Depends(get_db)):
    """The attached document, for a signed-in person only (the token is checked like on every other call)."""
    doc = db.query(EquipmentDocument).filter(EquipmentDocument.id == document_id).first()
    if doc is None:
        raise HTTPException(404, detail="Document not found")
    return Response(content=doc.content, media_type=doc.content_type or "application/octet-stream", headers={
        "Content-Disposition": f'inline; filename="{doc.filename}"',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
    })


@router.get("/stats", response_model=EquipmentStats)
def get_stats(db: Session = Depends(get_db)):
    return crud.get_stats(db)


@router.get("/inspections-due", response_model=list[InspectionDueItem])
def inspections_due(limit: int = Query(5, ge=1, le=50), db: Session = Depends(get_db)):
    """Assets ordered by their next (internal / external) inspection date, earliest first."""
    return crud.get_inspections_due(db, limit)


@router.get("/check-id/{equipment_id}")
def check_equipment_id(equipment_id: str, db: Session = Depends(get_db)):
    existing = crud.get_equipment_by_equipment_id(db, equipment_id)
    return {"exists": existing is not None}


@router.get("", response_model=EquipmentListResponse)
def list_equipment(
    search: Optional[str] = Query(None),
    equipment_type: Optional[str] = Query(None),
    plant: Optional[str] = Query(None),
    unit: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    criticality: Optional[str] = Query(None),
    sort_by: str = Query("equipment_id"),
    sort_order: str = Query("asc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
):
    items, total, total_pages = crud.get_equipment_list(
        db=db,
        search=search,
        equipment_type=equipment_type,
        plant=plant,
        unit=unit,
        status=status,
        criticality=criticality,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        page_size=page_size,
    )
    return EquipmentListResponse(
        data=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get("/{uuid}", response_model=EquipmentResponse)
def get_equipment(uuid: str, db: Session = Depends(get_db)):
    equipment = crud.get_equipment_by_uuid(db, uuid)
    if not equipment:
        raise HTTPException(status_code=404, detail="Equipment not found")
    return equipment


@router.post("", response_model=EquipmentResponse, status_code=201)
def create_equipment(data: EquipmentCreate, db: Session = Depends(get_db),
                     principal: Principal = Depends(authorize)):
    existing = crud.get_equipment_by_equipment_id(db, data.equipment_id)
    if existing:
        raise HTTPException(status_code=400, detail="Equipment ID already exists")
    data.created_by = principal.username          # the signed-in user, never what the client sends
    return crud.create_equipment(db, data, actor=principal.username)


@router.put("/{uuid}", response_model=EquipmentResponse)
def update_equipment(uuid: str, data: EquipmentUpdate, db: Session = Depends(get_db),
                     principal: Principal = Depends(authorize)):
    data.updated_by = principal.username
    try:
        equipment = crud.update_equipment(db, uuid, data, actor=principal.username, reason_required=settings.AUTH_REQUIRED)
    except crud.ReasonRequiredError as err:
        raise HTTPException(status_code=400, detail=str(err))
    if not equipment:
        raise HTTPException(status_code=404, detail="Equipment not found")
    return equipment


@router.get("/{uuid}/history", response_model=list[AuditOut])
def equipment_history(uuid: str, db: Session = Depends(get_db)):
    """Who created, changed or removed this equipment, and what exactly changed (old -> new), newest first."""
    equipment = crud.get_equipment_by_uuid(db, uuid)
    if not equipment:
        raise HTTPException(status_code=404, detail="Equipment not found")
    return db.query(AuditLog).filter(AuditLog.target == str(equipment.id), AuditLog.action.like("equipment.%"))         .order_by(AuditLog.at.desc()).all()


@router.delete("/{uuid}", response_model=EquipmentResponse)
def delete_equipment(uuid: str, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    equipment = crud.soft_delete_equipment(db, uuid, deleted_by=principal.username)
    if not equipment:
        raise HTTPException(status_code=404, detail="Equipment not found")
    return equipment
