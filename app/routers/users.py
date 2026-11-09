"""User management for admins."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AuditLog, User
from ..schemas import UserCreate, UserUpdate
from ..security import hash_password, new_salt, require_role
from .auth import user_dict

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("")
def list_users(db: Session = Depends(get_db), _: User = Depends(require_role("admin"))):
    return [user_dict(u) for u in db.scalars(select(User).order_by(User.id))]


@router.post("", status_code=201)
def create_user(body: UserCreate, db: Session = Depends(get_db), admin: User = Depends(require_role("admin"))):
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(422, "Enter a valid email address")
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(409, "A user with this email already exists")
    salt = new_salt()
    user = User(email=email, name=body.name.strip(), role=body.role, salt=salt, password_hash=hash_password(body.password, salt))
    db.add(user)
    db.add(AuditLog(actor=admin.email, action="user.create", entity="user", entity_id=email, details={"role": body.role}))
    db.commit()
    return user_dict(user)


def _admin_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.role == "admin")) or 0


@router.patch("/{user_id}")
def update_user(user_id: int, body: UserUpdate, db: Session = Depends(get_db), admin: User = Depends(require_role("admin"))):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if body.role and body.role != "admin" and user.role == "admin" and _admin_count(db) <= 1:
        raise HTTPException(409, "There must be at least one admin")
    if body.role:
        user.role = body.role
    if body.password:
        user.salt = new_salt()
        user.password_hash = hash_password(body.password, user.salt)
    db.add(AuditLog(actor=admin.email, action="user.update", entity="user", entity_id=user.email, details={"role": body.role, "password_changed": bool(body.password)}))
    db.commit()
    return user_dict(user)


@router.delete("/{user_id}", status_code=204)
def delete_user(user_id: int, db: Session = Depends(get_db), admin: User = Depends(require_role("admin"))):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if user.id == admin.id:
        raise HTTPException(409, "You cannot delete your own account")
    if user.role == "admin" and _admin_count(db) <= 1:
        raise HTTPException(409, "There must be at least one admin")
    db.add(AuditLog(actor=admin.email, action="user.delete", entity="user", entity_id=user.email, details={}))
    db.delete(user)
    db.commit()
