"""Small admin commands.

    python -m app.cli create-user you@example.com "Your Name" admin
    python -m app.cli list-users
"""

from __future__ import annotations

import getpass
import sys

from sqlalchemy import select

from .db import SessionLocal, init_db
from .models import User
from .security import hash_password, new_salt

ROLES = ("viewer", "tester", "qa_lead", "admin")


def create_user(email: str, name: str, role: str) -> None:
    if role not in ROLES:
        sys.exit(f"Role must be one of: {', '.join(ROLES)}")
    password = getpass.getpass("Password (min 8 characters): ")
    if len(password) < 8:
        sys.exit("Password is too short")
    init_db()
    with SessionLocal() as db:
        email = email.strip().lower()
        if db.scalar(select(User.id).where(User.email == email)):
            sys.exit("That email already has an account")
        salt = new_salt()
        db.add(User(email=email, name=name, role=role, salt=salt, password_hash=hash_password(password, salt)))
        db.commit()
    print(f"Created {role} {email}")


def list_users() -> None:
    init_db()
    with SessionLocal() as db:
        for user in db.scalars(select(User).order_by(User.id)):
            print(f"{user.id:>3}  {user.role:<8}  {user.email}  ({user.name})")


def main(argv: list[str]) -> None:
    if len(argv) == 5 and argv[1] == "create-user":
        create_user(argv[2], argv[3], argv[4])
    elif len(argv) == 2 and argv[1] == "list-users":
        list_users()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
