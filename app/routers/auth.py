from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..schemas import LoginBody
from ..security import create_token, get_current_user, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


def user_dict(user: User) -> dict:
    return {"id": user.id, "email": user.email, "name": user.name, "role": user.role}


@router.post("/login")
def login(body: LoginBody, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    if not user or not verify_password(body.password, user.salt, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    return {"access_token": create_token(user), "token_type": "bearer", "user": user_dict(user)}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return user_dict(user)


@router.get("/demo-users")
def demo_users(db: Session = Depends(get_db)):
    """Lets the login screen offer one-click demo accounts. Remove for real deployments."""
    return [user_dict(u) for u in db.scalars(select(User).order_by(User.id))]
