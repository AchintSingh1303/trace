"""Demo data. Run `python -m app.seed --reset` for a clean slate."""

from __future__ import annotations

import sys

from sqlalchemy import select

from .db import Base, SessionLocal, engine, init_db
from .models import Cycle, Project, Requirement, User
from .security import hash_password, new_salt

DEMO_PASSWORD = "demo123"
DEMO_USERS = [
    ("admin@demo.local", "Alex Admin", "admin"),
    ("lead@demo.local", "Lena Lead", "qa_lead"),
    ("tester@demo.local", "Tariq Tester", "tester"),
    ("viewer@demo.local", "Vera Viewer", "viewer"),
]

DEMO_REQUIREMENTS = [
    ("Functional", "User login", "Registered users sign in with email and password.", [
        "A registered user with valid credentials lands on the dashboard",
        "An incorrect password shows an error without revealing which field was wrong",
        "The account is locked after 5 failed attempts",
        "The password must be at least 8 characters",
    ]),
    ("Functional", "Add to cart", "Shoppers add products to a cart from the product page.", [
        "Adding an item increases the cart count",
        "Quantity is limited to a maximum of 10 per item",
        "An out of stock item cannot be added to the cart",
    ]),
    ("Functional", "Checkout and payment", "Customers pay for the items in their cart with a card.", [
        "A valid card completes the order and shows a confirmation number",
        "An expired card is rejected with a clear message",
        "A valid coupon code reduces the order total",
    ]),
    ("UI", "Product search", "Shoppers search the catalogue by keyword.", [
        "Search results update as the shopper types",
        "No results shows a helpful empty state",
    ]),
    ("API", "POST /orders creates an order", "Creates an order from the current cart. POST /orders", [
        "A valid request returns 201 with the new order id",
        "An empty cart is rejected with a validation error",
    ]),
    ("API", "GET /products/{id} returns a product", "Returns product details. GET /products/{id}", [
        "An existing product returns its name, price and stock",
        "The list endpoint returns at most 100 results per page",
    ]),
    ("Security", "Session and access control", "Protects customer accounts and order history.", [
        "A customer can only view their own orders",
        "Sessions expire after 30 minutes of inactivity",
    ]),
    ("Security", "Input handling and sensitive data", "Protects the platform from malicious input and protects card data.", [
        "All free-text input is validated and sanitised before storage",
        "Card numbers are never written to logs",
    ]),
    ("Performance", "Search latency", "Search must stay fast under load: p95 under 800 ms at 200 concurrent users.", [
        "Search p95 latency is under 800 ms at 200 concurrent users",
    ]),
    ("Performance", "Checkout throughput", "The checkout flow must sustain peak sale traffic with a defined limit: 50 orders per minute.", [
        "Checkout sustains 50 orders per minute with p95 under 1.5 s",
    ]),
    ("Journey", "Guest to paid order", "A new visitor finds a product and completes an order.", [
        "Find a product with search",
        "Add the product to the cart",
        "Sign in or register at checkout",
        "Pay and receive an order confirmation",
    ]),
]


def seed_users(db) -> None:
    if db.scalar(select(User.id)):
        return
    for email, name, role in DEMO_USERS:
        salt = new_salt()
        db.add(User(email=email, name=name, role=role, salt=salt, password_hash=hash_password(DEMO_PASSWORD, salt)))
    db.commit()


def seed_demo_project(db) -> None:
    if db.scalar(select(Project.id)):
        return
    project = Project(
        name="Demo Shop",
        description="A small online shop used to demonstrate test case generation.",
        app_type="web",
    )
    db.add(project)
    db.flush()
    cycle = Cycle(project_id=project.id, name="Sprint 1")
    db.add(cycle)
    db.flush()
    for index, (req_type, title, description, criteria) in enumerate(DEMO_REQUIREMENTS, start=1):
        db.add(
            Requirement(
                cycle_id=cycle.id,
                code=f"RQ-{index:03d}",
                title=title,
                description=description,
                req_type=req_type,
                acceptance_criteria=criteria,
            )
        )
    db.commit()


def bootstrap_admin() -> bool:
    """Create the first admin from ADMIN_EMAIL / ADMIN_PASSWORD when the user table is empty."""
    from .config import settings

    if not (settings.admin_email and settings.admin_password):
        return False
    with SessionLocal() as db:
        if db.scalar(select(User.id)):
            return False
        salt = new_salt()
        db.add(User(email=settings.admin_email, name="Administrator", role="admin", salt=salt, password_hash=hash_password(settings.admin_password, salt)))
        db.commit()
    return True


def seed(with_project: bool = True) -> None:
    init_db()
    with SessionLocal() as db:
        seed_users(db)
        if with_project:
            seed_demo_project(db)


if __name__ == "__main__":
    if "--reset" in sys.argv:
        Base.metadata.drop_all(engine)
        print("Dropped all tables.")
    seed()
    print("Seeded demo users and the Demo Shop project.")
    print(f"Log in with any of: {', '.join(e for e, _, _ in DEMO_USERS)} / password: {DEMO_PASSWORD}")
