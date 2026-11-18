import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import Principal, authorize
from app.core.database import get_db
from app.crud import rbi_assessment as crud

router = APIRouter()

# Rights (api/deps.py decides them from the method and path): everybody signed in reads; an Inspector (engineer) creates components
# and works on assessments and finalizes them; an Admin approves or returns them (never the ones he finalized himself) and deletes
# components; a Super Admin only looks.


class Body(BaseModel):
    inputs: Dict[str, Any] = {}
    assessment_date: Optional[str] = None
    comments: Optional[str] = None


class ComponentBody(BaseModel):
    name: Optional[str] = None
    component_type: Optional[str] = None
    in_service_date: Optional[str] = None
    status: Optional[str] = None
    description: Optional[str] = None
    comments: Optional[str] = None


class ReturnBody(BaseModel):
    reason: Optional[str] = None


class FinalizeBody(BaseModel):
    approver: Optional[str] = None                              # the admin it is sent to (required when people sign in)
    comment: Optional[str] = None


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except LookupError as err:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(err).strip("'\""))
    except PermissionError as err:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(err))
    except ValueError as err:                                   # what the person wrote is not acceptable
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(err))


@router.get("/form")
def form_description():
    """The workbook's inputs, factors, weights, matrix, priorities and known issues: what the screens need to draw the form."""
    return crud.form_description()


@router.get("/fluids")
def fluids(kind: str = Query("flammable", pattern="^(toxic|flammable)$"), q: Optional[str] = Query(None),
           limit: int = Query(50, ge=1, le=500), db: Session = Depends(get_db)):
    return crud.list_fluids(db, q, kind, limit)


@router.get("/mechanisms")
def mechanisms(location: Optional[str] = Query(None, pattern="^(Internal|External)$"), q: Optional[str] = Query(None),
               db: Session = Depends(get_db)):
    return crud.list_mechanisms(db, location, q)


@router.get("/equipment")
def list_equipment(q: Optional[str] = Query(None), equipment_type: Optional[str] = Query(None), page: int = Query(1, ge=1),
                   page_size: int = Query(25, ge=1, le=100), sort: str = Query("tag", pattern="^(tag|priority)$"),
                   risk_level: Optional[str] = Query(None, max_length=40), db: Session = Depends(get_db)):
    return crud.list_equipment(db, q, equipment_type, page, page_size, sort, risk_level)


@router.get("/equipment/{equipment_id}")
def get_equipment(equipment_id: uuid.UUID, db: Session = Depends(get_db)):
    return _run(crud.get_equipment, db, equipment_id)


@router.post("/equipment/{equipment_id}/components", status_code=status.HTTP_201_CREATED)
def create_component(equipment_id: uuid.UUID, body: ComponentBody, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    return _run(crud.create_component, db, equipment_id, body.model_dump(exclude_unset=True), principal.username)


@router.get("/components/{component_id}")
def get_component(component_id: uuid.UUID, db: Session = Depends(get_db)):
    return _run(crud.get_component, db, component_id)


@router.put("/components/{component_id}")
def update_component(component_id: uuid.UUID, body: ComponentBody, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    return _run(crud.update_component, db, component_id, body.model_dump(exclude_unset=True), principal.username)


@router.delete("/components/{component_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_component(component_id: uuid.UUID, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    _run(crud.delete_component, db, component_id, principal.username)


@router.post("/components/{component_id}/assessments", status_code=status.HTTP_201_CREATED)
def new_assessment(component_id: uuid.UUID, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    """A new draft: a copy of the last one, or the values the equipment register already has for the first one."""
    return _run(crud.new_assessment, db, component_id, principal.username)


@router.get("/assessments/{assessment_id}")
def get_assessment(assessment_id: uuid.UUID, db: Session = Depends(get_db)):
    return _run(crud.get_assessment, db, assessment_id)


@router.post("/assessments/{assessment_id}/preview")
def preview(assessment_id: uuid.UUID, body: Body, db: Session = Depends(get_db)):
    """The result of what is typed so far; nothing is saved."""
    return _run(crud.preview, db, assessment_id, body.model_dump())


@router.put("/assessments/{assessment_id}")
def save_assessment(assessment_id: uuid.UUID, body: Body, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    return _run(crud.save_assessment, db, assessment_id, body.model_dump(exclude_unset=True), principal.username)


@router.delete("/assessments/{assessment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_assessment(assessment_id: uuid.UUID, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    _run(crud.delete_assessment, db, assessment_id, principal.username)


@router.post("/assessments/{assessment_id}/finalize")
def finalize(assessment_id: uuid.UUID, body: Optional[FinalizeBody] = None, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    body = body or FinalizeBody()
    return _run(crud.finalize, db, assessment_id, principal.username, body.approver, body.comment)


@router.post("/assessments/{assessment_id}/withdraw")
def withdraw(assessment_id: uuid.UUID, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    return _run(crud.withdraw, db, assessment_id, principal.username, principal.role)


@router.post("/assessments/{assessment_id}/approve")
def approve(assessment_id: uuid.UUID, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    return _run(crud.approve, db, assessment_id, principal.username)


@router.post("/assessments/{assessment_id}/return")
def return_assessment(assessment_id: uuid.UUID, body: ReturnBody, db: Session = Depends(get_db), principal: Principal = Depends(authorize)):
    return _run(crud.return_assessment, db, assessment_id, body.reason, principal.username)


@router.get("/matrix")
def risk_matrix(db: Session = Depends(get_db)):
    return crud.risk_matrix(db)


@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    return crud.summary(db)
