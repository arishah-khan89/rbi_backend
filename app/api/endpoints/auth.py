"""Login, current user, password change and (super admin only) user management and the audit trail."""
import csv
import io
import time
from collections import defaultdict, deque
from datetime import datetime, time as dtime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import Principal, authorize
from app.core import audit
from app.core.config import settings
from app.core.database import get_db
from app.core.security import (ROLES, create_access_token, hash_password, validate_new_password,
                               verify_password)
from app.core.approval import PENDING
from app.models.audit import AuditLog
from app.models.inspection import InspectionRecord
from app.models.user import User
from app.schemas.auth import (AuditOut, AuditPage, ChangePasswordRequest, LoginRequest, TokenResponse, UserCreate, UserOut,
                              UserUpdate)

public_router = APIRouter(prefix="/auth", tags=["Auth"])      # no token needed
router = APIRouter(prefix="/auth", tags=["Auth"])             # token needed (see api/router.py)

# --- brute-force protection: max failed sign-ins per user+address in a time window ---
MAX_FAILURES, WINDOW_SECONDS = 5, 300
_failures: dict = defaultdict(deque)
_DUMMY_HASH = hash_password("not-a-real-password")           # keeps timing similar for unknown users


def _throttle_key(username: str, request: Request) -> str:
    return f"{username.lower()}|{request.client.host if request.client else '-'}"


def _recent_failures(key: str) -> deque:
    q = _failures[key]
    cutoff = time.monotonic() - WINDOW_SECONDS
    while q and q[0] < cutoff:
        q.popleft()
    return q


@public_router.post("/login", response_model=TokenResponse)
def login(data: LoginRequest, request: Request, db: Session = Depends(get_db)):
    key = _throttle_key(data.username, request)
    if len(_recent_failures(key)) >= MAX_FAILURES:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="Too many failed attempts. Please wait a few minutes and try again.")
    user = db.query(User).filter(User.username == data.username.strip().lower()).first()
    ok = verify_password(data.password, user.password_hash if user else _DUMMY_HASH)
    if not user or not ok or not user.is_active:
        _failures[key].append(time.monotonic())
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Wrong username or password.")
    _failures.pop(key, None)
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    return TokenResponse(
        access_token=create_access_token(user.username, user.role, password_hash=user.password_hash),
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserOut.model_validate(user),
    )


@router.get("/me", response_model=UserOut)
def me(principal: Principal = Depends(authorize), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == principal.username).first()
    if user is None:     # AUTH_REQUIRED=false (anonymous principal)
        return UserOut(id="00000000-0000-0000-0000-000000000000", username=principal.username,
                       full_name=None, role=principal.role, is_active=True)
    return user


@router.post("/change-password")
def change_password(data: ChangePasswordRequest, principal: Principal = Depends(authorize),
                    db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == principal.username).first()
    if user is None or not verify_password(data.current_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Current password is wrong.")
    problem = validate_new_password(data.new_password)
    if problem:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=problem)
    if verify_password(data.new_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="The new password must be different from the current one.")
    user.password_hash = hash_password(data.new_password)
    user.must_change_password = False
    audit.record(db, user.username, "password.change", user.username)
    db.commit()
    # every token issued before this change stops working; the person gets a fresh one so this session carries on
    return {"status": "ok", "access_token": create_access_token(user.username, user.role, password_hash=user.password_hash),
            "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60}


# ---------------------------------------------------------------- super admin: users and audit trail
def _active_super_admins_besides(db: Session, user: User) -> int:
    return db.query(User).filter(User.role == "super_admin", User.is_active.is_(True), User.id != user.id).count()


@router.get("/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)):
    return db.query(User).order_by(User.username).all()


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(data: UserCreate, principal: Principal = Depends(authorize), db: Session = Depends(get_db)):
    """The password given here is a temporary one: the person must choose their own at the first sign-in."""
    if data.role not in ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"Role must be one of: {', '.join(ROLES)}.")
    problem = validate_new_password(data.password)
    if problem:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=problem)
    username = data.username.lower()
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="This username already exists.")
    user = User(username=username, full_name=data.full_name, role=data.role,
                password_hash=hash_password(data.password), must_change_password=True)
    db.add(user)
    audit.record(db, principal.username, "user.create", username, f"role={data.role}")
    db.commit()
    db.refresh(user)
    return user


@router.patch("/users/{username}", response_model=UserOut)
def update_user(username: str, data: UserUpdate, principal: Principal = Depends(authorize),
                db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == username.lower()).first()
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.")
    changes = data.model_dump(exclude_unset=True)
    if "role" in changes and changes["role"] not in ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"Role must be one of: {', '.join(ROLES)}.")

    # nobody can lock themselves out or take their own authority away
    if user.username == principal.username and (
            ("role" in changes and changes["role"] != user.role) or changes.get("is_active") is False):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="You cannot change your own role or deactivate yourself.")
    # there must always be one active super admin
    losing_super = user.role == "super_admin" and (
        changes.get("role", "super_admin") != "super_admin" or changes.get("is_active") is False)
    if losing_super and _active_super_admins_besides(db, user) == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="You cannot remove the last active super admin.")

    if changes.get("new_password"):
        problem = validate_new_password(changes["new_password"])
        if problem:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=problem)
        user.password_hash = hash_password(changes["new_password"])
        user.must_change_password = True          # a reset password is temporary too
        audit.record(db, principal.username, "user.password_reset", user.username)
    if "role" in changes and changes["role"] != user.role:
        audit.record(db, principal.username, "user.role", user.username, f"{user.role} -> {changes['role']}")
    if "is_active" in changes and changes["is_active"] != user.is_active:
        audit.record(db, principal.username, "user.activate" if changes["is_active"] else "user.deactivate", user.username)
    if "full_name" in changes and changes["full_name"] != user.full_name:
        audit.record(db, principal.username, "user.name", user.username, f"{user.full_name or '-'} -> {changes['full_name'] or '-'}")
    for field in ("full_name", "role", "is_active"):
        if field in changes:
            setattr(user, field, changes[field])
    db.commit()
    db.refresh(user)
    return user


@router.delete("/users/{username}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(username: str, principal: Principal = Depends(authorize), db: Session = Depends(get_db)):
    """Removes the account. Inspections and audit lines keep the person's name (they only store it as text).
    Deactivating is the gentler way when the person may come back."""
    user = db.query(User).filter(User.username == username.lower()).first()
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.")
    if user.username == principal.username:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="You cannot delete your own account.")
    if user.role == "super_admin" and user.is_active and _active_super_admins_besides(db, user) == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="You cannot delete the last active super admin.")
    waiting = db.query(InspectionRecord).filter(InspectionRecord.approval_status == PENDING,
                                                InspectionRecord.assigned_reviewer == user.username).count()
    if waiting:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=(
            f"{user.username} still has {waiting} inspection(s) waiting for approval. Let them decide, or ask the "
            f"sender to withdraw and send to another admin, then delete this user."))
    audit.record(db, principal.username, "user.delete", user.username, f"role={user.role}")
    db.delete(user)
    db.commit()


@router.get("/reviewers")
def list_reviewers(db: Session = Depends(get_db)):
    """Names only: the active administrators an inspection can be sent to (any signed-in person may ask)."""
    from app.crud.approval import reviewers
    return [{"username": u.username, "full_name": u.full_name, "role": u.role} for u in reviewers(db)]


def _audit_query(db: Session, actor: Optional[str], action: Optional[str], q: Optional[str],
                 date_from: Optional[str], date_to: Optional[str]):
    """The audit lines that match the filters (newest first)."""
    query = db.query(AuditLog)
    if actor:
        query = query.filter(AuditLog.actor.ilike(f"%{actor.strip()}%"))
    if action:
        query = query.filter(AuditLog.action.like(f"{action.strip()}%"))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(AuditLog.target.ilike(like) | AuditLog.detail.ilike(like) | AuditLog.action.ilike(like))
    try:
        if date_from:
            query = query.filter(AuditLog.at >= datetime.combine(datetime.strptime(date_from, "%Y-%m-%d").date(), dtime.min))
        if date_to:
            query = query.filter(AuditLog.at <= datetime.combine(datetime.strptime(date_to, "%Y-%m-%d").date(), dtime.max))
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Dates must be written as YYYY-MM-DD.")
    return query.order_by(AuditLog.at.desc())


def csv_safe(value) -> str:
    """A spreadsheet runs a cell that starts with = + - @ as a formula: such a cell gets a leading apostrophe."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@router.get("/audit/search", response_model=AuditPage)
def search_audit(
    actor: Optional[str] = None, action: Optional[str] = None, q: Optional[str] = None,
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    query = _audit_query(db, actor, action, q, date_from, date_to)
    return AuditPage(total=query.count(), items=query.offset(offset).limit(limit).all())


@router.get("/audit/export")
def export_audit(
    actor: Optional[str] = None, action: Optional[str] = None, q: Optional[str] = None,
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    principal: Principal = Depends(authorize), db: Session = Depends(get_db),
):
    """The filtered audit lines as a CSV file (at most 50,000). The export itself is written to the audit trail."""
    rows = _audit_query(db, actor, action, q, date_from, date_to).limit(50000).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["When", "By", "Action", "Target", "Detail"])
    for r in rows:
        writer.writerow([r.at.strftime("%Y-%m-%d %H:%M:%S"), csv_safe(r.actor), csv_safe(r.action), csv_safe(r.target), csv_safe(r.detail)])
    audit.record(db, principal.username, "audit.export", None, f"{len(rows)} line(s)")
    db.commit()
    name = f"audit_log_{datetime.now(timezone.utc):%Y%m%d_%H%M}.csv"
    return StreamingResponse(iter(["\ufeff" + buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/audit", response_model=list[AuditOut])
def list_audit(limit: int = 100, db: Session = Depends(get_db)):
    """The most recent audit lines, newest first."""
    return db.query(AuditLog).order_by(AuditLog.at.desc()).limit(max(1, min(limit, 500))).all()
