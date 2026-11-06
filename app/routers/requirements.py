import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..events import bus
from ..models import AuditLog, Cycle, Requirement, TestCase, Traceability, User
from ..schemas import RequirementCreate, RequirementUpdate
from ..security import get_current_user, require_role
from ..services.reporting import req_to_dict, requirement_stats

router = APIRouter(prefix="/api/cycles/{cycle_id}/requirements", tags=["requirements"])


def _cycle_or_404(db: Session, cycle_id: int) -> Cycle:
    cycle = db.get(Cycle, cycle_id)
    if not cycle:
        raise HTTPException(404, "Cycle not found")
    return cycle


def _next_code(db: Session, cycle_id: int) -> str:
    highest = 0
    for (code,) in db.execute(select(Requirement.code).where(Requirement.cycle_id == cycle_id)):
        match = re.match(r"RQ-(\d+)$", code)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"RQ-{highest + 1:03d}"


def _guard_running(cycle_id: int) -> None:
    if bus.is_active(cycle_id):
        raise HTTPException(409, "Test case generation is running for this cycle; try again when it finishes")


@router.get("")
def list_requirements(cycle_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    _cycle_or_404(db, cycle_id)
    stats = requirement_stats(db, cycle_id)
    reqs = db.scalars(select(Requirement).where(Requirement.cycle_id == cycle_id).order_by(Requirement.code)).all()
    return [req_to_dict(r, len(stats[r.id]["stale"]) if r.id in stats else 0, len(stats[r.id]["cases"]) if r.id in stats else 0) for r in reqs]


@router.post("", status_code=201)
def create_requirement(cycle_id: int, body: RequirementCreate, db: Session = Depends(get_db), user: User = Depends(require_role("qa_lead"))):
    _cycle_or_404(db, cycle_id)
    code = (body.code or _next_code(db, cycle_id)).strip().upper()
    if db.scalar(select(Requirement.id).where(Requirement.cycle_id == cycle_id, Requirement.code == code)):
        raise HTTPException(409, f"Requirement {code} already exists in this cycle")
    req = Requirement(
        cycle_id=cycle_id,
        code=code,
        title=body.title.strip(),
        description=body.description,
        req_type=body.req_type,
        acceptance_criteria=body.acceptance_criteria,
    )
    db.add(req)
    db.add(AuditLog(cycle_id=cycle_id, actor=user.email, action="requirement.create", entity="requirement", entity_id=code, details={"title": req.title}))
    db.commit()
    return req_to_dict(req)


@router.patch("/{req_id}")
def update_requirement(cycle_id: int, req_id: int, body: RequirementUpdate, db: Session = Depends(get_db), user: User = Depends(require_role("qa_lead"))):
    req = db.get(Requirement, req_id)
    if not req or req.cycle_id != cycle_id:
        raise HTTPException(404, "Requirement not found")
    _guard_running(cycle_id)

    changes = body.model_dump(exclude_unset=True)
    changed = {k: v for k, v in changes.items() if v is not None and getattr(req, k) != v}
    for key, value in changed.items():
        setattr(req, key, value)
    if changed:
        req.version += 1
        db.add(
            AuditLog(
                cycle_id=cycle_id,
                actor=user.email,
                action="requirement.update",
                entity="requirement",
                entity_id=req.code,
                details={"fields": sorted(changed), "new_version": req.version},
            )
        )
    db.commit()
    stats = requirement_stats(db, cycle_id).get(req.id, {"cases": set(), "stale": set()})
    return req_to_dict(req, len(stats["stale"]), len(stats["cases"]))


@router.delete("/{req_id}", status_code=204)
def delete_requirement(cycle_id: int, req_id: int, db: Session = Depends(get_db), user: User = Depends(require_role("qa_lead"))):
    req = db.get(Requirement, req_id)
    if not req or req.cycle_id != cycle_id:
        raise HTTPException(404, "Requirement not found")
    _guard_running(cycle_id)
    # cases generated for this requirement go with it; cases that merely also cover it stay
    owned = {
        row[0]
        for row in db.execute(
            select(Traceability.tc_id).where(Traceability.requirement_id == req_id, Traceability.is_primary.is_(True))
        )
    }
    db.add(AuditLog(cycle_id=cycle_id, actor=user.email, action="requirement.delete", entity="requirement", entity_id=req.code, details={"title": req.title, "removed_cases": len(owned)}))
    if owned:
        db.execute(delete(TestCase).where(TestCase.id.in_(owned)))
    db.delete(req)
    db.commit()
