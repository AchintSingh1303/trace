from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(String(128))
    salt: Mapped[str] = mapped_column(String(64))


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    app_type: Mapped[str] = mapped_column(String(50), default="web")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    cycles: Mapped[list["Cycle"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )


class Cycle(Base):
    __tablename__ = "cycles"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="cycles")


class Requirement(Base):
    __tablename__ = "requirements"
    __table_args__ = (UniqueConstraint("cycle_id", "code", name="uq_requirement_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("cycles.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    req_type: Mapped[str] = mapped_column(String(30))
    acceptance_criteria: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class GenerationRun(Base):
    __tablename__ = "generation_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("cycles.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    mode: Mapped[str] = mapped_column(String(20), default="full")
    selected_types: Mapped[list] = mapped_column(JSON, default=list)
    provider: Mapped[str] = mapped_column(String(30), default="rules")
    started_by: Mapped[str] = mapped_column(String(255), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class TestCase(Base):
    __tablename__ = "test_cases"
    __test__ = False  # not a pytest test class

    id: Mapped[int] = mapped_column(primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("cycles.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("generation_runs.id", ondelete="SET NULL"), nullable=True
    )
    tc_code: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(500))
    tag: Mapped[str] = mapped_column(String(100), default="")
    test_type: Mapped[str] = mapped_column(String(30), index=True)
    subtype: Mapped[str] = mapped_column(String(50), default="")
    priority: Mapped[str] = mapped_column(String(20), default="medium")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    preconditions: Mapped[list] = mapped_column(JSON, default=list)
    automation_status: Mapped[str] = mapped_column(String(30), default="not_started")
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    steps: Mapped[list["TCStep"]] = relationship(
        cascade="all, delete-orphan", order_by="TCStep.seq", passive_deletes=True
    )
    test_data: Mapped[list["TCTestData"]] = relationship(
        cascade="all, delete-orphan", order_by="TCTestData.id", passive_deletes=True
    )
    links: Mapped[list["Traceability"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True
    )


class TCStep(Base):
    __tablename__ = "tc_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    tc_id: Mapped[int] = mapped_column(ForeignKey("test_cases.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(Text)
    expected: Mapped[str] = mapped_column(Text, default="")


class TCTestData(Base):
    __tablename__ = "tc_test_data"

    id: Mapped[int] = mapped_column(primary_key=True)
    tc_id: Mapped[int] = mapped_column(ForeignKey("test_cases.id", ondelete="CASCADE"), index=True)
    field_name: Mapped[str] = mapped_column(String(255))
    value: Mapped[str] = mapped_column(String(500))
    step_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Traceability(Base):
    """Requirement <-> test case link. Records which requirement version the case was written against."""

    __tablename__ = "traceability"
    __table_args__ = (Index("ix_trace_req", "requirement_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("cycles.id", ondelete="CASCADE"), index=True)
    requirement_id: Mapped[int] = mapped_column(ForeignKey("requirements.id", ondelete="CASCADE"))
    tc_id: Mapped[int] = mapped_column(ForeignKey("test_cases.id", ondelete="CASCADE"), index=True)
    req_version: Mapped[int] = mapped_column(Integer, default=1)
    # The requirement the case was generated for. Other links are "also covers".
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    cycle_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(80))
    entity: Mapped[str] = mapped_column(String(50), default="")
    entity_id: Mapped[str] = mapped_column(String(50), default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
