from fastapi import APIRouter, Depends
from app.api.deps import authorize
from app.api.endpoints import equipment, inspection, cml, asme, auth, tank, notifications, inspection_tasks, inspection_reports, rbi_assessment

api_router = APIRouter(prefix="/api/v1")

# Public: only the sign-in endpoint
api_router.include_router(auth.public_router)

# Everything else requires a valid token and the role that the method/path needs (see api/deps.py)
protected = APIRouter(dependencies=[Depends(authorize)])
protected.include_router(auth.router)
protected.include_router(equipment.router)
protected.include_router(inspection.router, prefix="/inspection", tags=["Inspection Register"])
protected.include_router(cml.router, prefix="/cml", tags=["CML / Thickness Readings"])
protected.include_router(asme.router)
protected.include_router(tank.router)
protected.include_router(notifications.router, prefix="/notifications", tags=["Notifications"])
protected.include_router(inspection_tasks.router, prefix="/inspection-tasks", tags=["Inspection Tasks"])
protected.include_router(inspection_reports.router, prefix="/inspection-reports", tags=["Inspection Reports"])
protected.include_router(rbi_assessment.router, prefix="/risk-assessment", tags=["Risk Assessment (RBI)"])
api_router.include_router(protected)
