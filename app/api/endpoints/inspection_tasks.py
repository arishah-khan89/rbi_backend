import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import Principal, authorize
from app.core.database import get_db
from app.crud import inspection_task as crud
from app.crud import rbi_tasks
from app.models.audit import AuditLog
from app.schemas.auth import AuditOut
from app.schemas.inspection_task import TaskCreate, TaskListResponse, TaskResponse, TaskUpdate

router = APIRouter()


def _bad(err: ValueError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err))


@router.get("", response_model=TaskListResponse)
def list_tasks(
    equipment_id: Optional[uuid.UUID] = Query(None, description="Only the tasks of this equipment"),
    task_type: Optional[str] = Query(None, description="Internal, External, UTG or NDE"),
    status_filter: Optional[str] = Query(None, alias="status", description="overdue, due_soon, ok or none"),
    q: Optional[str] = Query(None, description="Search the name, details or equipment"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
):
    return crud.list_tasks(db, equipment_id, task_type, status_filter, q, page, page_size)


@router.get("/rbi-suggestion")
def rbi_suggestion(equipment_id: uuid.UUID = Query(...), task_type: str = Query(...), db: Session = Depends(get_db)):
    """The RBI interval (and last / next date) the approved risk assessments of this equipment give for an Internal / External task."""
    found = rbi_tasks.suggestion(db, equipment_id, task_type)
    return found or {"interval_years": None}


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(task_id: uuid.UUID, db: Session = Depends(get_db)):
    task = crud.get_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Inspection task not found")
    return task


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
def create_task(data: TaskCreate, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    try:
        return crud.create_task(db, data, actor=principal.username)
    except ValueError as err:
        raise _bad(err)


@router.put("/{task_id}", response_model=TaskResponse)
def update_task(task_id: uuid.UUID, data: TaskUpdate, db: Session = Depends(get_db),
                principal: Principal = Depends(authorize)):
    try:
        task = crud.update_task(db, task_id, data, actor=principal.username)
    except ValueError as err:
        raise _bad(err)
    if task is None:
        raise HTTPException(status_code=404, detail="Inspection task not found")
    return task


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(task_id: uuid.UUID, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    try:
        deleted = crud.delete_task(db, task_id, actor=principal.username)
    except ValueError as err:
        raise _bad(err)
    if not deleted:
        raise HTTPException(status_code=404, detail="Inspection task not found")


@router.get("/{task_id}/history", response_model=list[AuditOut])
def task_history(task_id: uuid.UUID, db: Session = Depends(get_db)):
    """Who created, changed or removed this task, and what changed (old -> new), newest first."""
    return db.query(AuditLog).filter(AuditLog.target == str(task_id), AuditLog.action.like("task.%")) \
        .order_by(AuditLog.at.desc()).all()
