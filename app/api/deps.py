"""Authentication and role-based authorization for every protected route.

Roles (low -> high):  viewer < engineer < admin < super_admin
(shown on screen as: Viewer, Inspector, Admin, Super Admin)
  viewer       read everything, run calculations
  engineer     + create / edit equipment, inspections, imports, uploads            (Inspector)
  admin        + review inspections (approve / return / reopen the ones sent to them) and delete
  super_admin  creates the admins and users, manages roles, reads the audit trail and looks at everything; does NOT
               enter, change or delete inspection / equipment data (it may only withdraw or reopen an inspection)
The rule is decided in ONE place (required_role) from the HTTP method and path, so a new endpoint is
protected automatically.
"""
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import decode_access_token, password_version
from app.models.user import User

ROLE_RANK = {"viewer": 1, "engineer": 2, "admin": 3, "super_admin": 4}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
COMPUTE_ONLY_POSTS = {"/api/v1/asme/calculate-tmin", "/api/v1/tank/calculate"}          # calculations that store nothing
SELF_SERVICE_PATHS = {"/api/v1/auth/change-password"}          # any signed-in user
SUPER_ADMIN_PREFIXES = ("/api/v1/auth/users", "/api/v1/auth/audit")
SELF_SERVICE_PREFIXES = ("/api/v1/notifications",)      # a person's own notifications: any signed-in person, any role
INSPECTION_PREFIX = "/api/v1/inspection"
REPORT_PREFIX = "/api/v1/inspection-reports"
RISK_PREFIX = "/api/v1/risk-assessment"
REVIEW_ACTIONS = {"approve", "return", "reopen", "reject"}
# the only calls a person may make before they have chosen their own password
PASSWORD_PENDING_PATHS = {"/api/v1/auth/change-password", "/api/v1/auth/me"}

bearer = HTTPBearer(auto_error=False)


@dataclass
class Principal:
    username: str
    role: str
    full_name: Optional[str] = None
    user_id: Optional[str] = None


def required_role(method: str, path: str) -> str:
    path = path.rstrip("/")
    if path.startswith(SUPER_ADMIN_PREFIXES):
        return "super_admin"
    # approval workflow: only admins (and the super admin) see the waiting list and decide
    # (the per-person count is for everybody)
    if path.startswith(INSPECTION_PREFIX + "/approvals") and not path.endswith("/summary"):
        return "admin"
    if method.upper() == "POST" and path.startswith(INSPECTION_PREFIX + "/") and path.rsplit("/", 1)[-1] in REVIEW_ACTIONS:
        return "admin"
    if path.startswith(REPORT_PREFIX + "/"):
        last = path.rsplit("/", 1)[-1]
        if method.upper() == "POST" and last in REVIEW_ACTIONS:
            return "admin"                                    # approve / return / reopen a report
        if method.upper() == "DELETE" and "/files/" in path:
            return "engineer"                                 # taking a file off one's own report
    if path.startswith(RISK_PREFIX + "/"):
        if method.upper() == "POST" and path.rsplit("/", 1)[-1] in ("approve", "return"):
            return "admin"                                    # an admin decides on a finalized risk assessment
        if method.upper() == "DELETE" and "/assessments/" in path:
            return "engineer"                                 # taking back one's own draft (the server only allows drafts)
        if method.upper() == "POST" and path.endswith("/preview"):
            return "viewer"                                   # a calculation that stores nothing
    if path in SELF_SERVICE_PATHS or path.startswith(SELF_SERVICE_PREFIXES) or path in COMPUTE_ONLY_POSTS \
            or method.upper() in SAFE_METHODS:
        return "viewer"
    if method.upper() == "DELETE":
        return "admin"
    return "engineer"


SUPER_ADMIN_OVERSIGHT = {"withdraw", "reopen"}       # the only inspection actions a super admin takes (nobody is stuck on leave)


def super_admin_may(method: str, path: str) -> bool:
    """A super admin runs the system (users, roles, audit trail) and looks at everything, but does not enter, change or
    delete inspection or equipment data: whoever runs the system is not the one who produces the data."""
    path = path.rstrip("/")
    if method.upper() in SAFE_METHODS or path in COMPUTE_ONLY_POSTS or path.startswith(("/api/v1/auth/", *SELF_SERVICE_PREFIXES)):
        return True
    if method.upper() == "POST" and path.startswith(RISK_PREFIX + "/") and path.endswith("/preview"):
        return True                                                # looking at a result stores nothing
    return (method.upper() == "POST" and path.startswith((INSPECTION_PREFIX + "/", REPORT_PREFIX + "/"))
            and path.rsplit("/", 1)[-1] in SUPER_ADMIN_OVERSIGHT)


def authorize(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    db: Session = Depends(get_db),
) -> Principal:
    if not settings.AUTH_REQUIRED:
        return Principal(username="System", role="admin")

    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Please sign in.",
                                 headers={"WWW-Authenticate": "Bearer"})
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized
    payload = decode_access_token(credentials.credentials)
    if not payload or not payload.get("sub"):
        raise unauthorized

    user = db.query(User).filter(User.username == str(payload["sub"]).lower()).first()
    if user is None or not user.is_active:
        raise unauthorized
    if payload.get("pv") != password_version(user.password_hash):      # the password was changed or reset after this sign-in
        raise unauthorized

    if user.must_change_password and request.url.path.rstrip("/") not in PASSWORD_PENDING_PATHS:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="You must set a new password before you continue.")

    need = required_role(request.method, request.url.path)
    if ROLE_RANK.get(user.role, 0) < ROLE_RANK[need]:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            detail=f"Your role ({user.role}) is not allowed to do this. Required: {need}.")
    if user.role == "super_admin" and not super_admin_may(request.method, request.url.path):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=(
            "A Super Admin manages users and reads the audit log, and does not enter, change or delete inspection or "
            "equipment data. Use an Admin or Inspector account for that."))
    return Principal(username=user.username, role=user.role, full_name=user.full_name, user_id=str(user.id))
