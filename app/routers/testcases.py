from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..models import AuditLog, Cycle, TCStep, TestCase, User
from ..schemas import BulkReviewBody, CaseEdit, ReviewBody
from ..security import get_current_user, require_role
from ..services.reporting import _load_req_map, export_csv, list_cases, tc_to_dict, traceability_matrix

router = APIRouter(prefix="/api/cycles/{cycle_id}", tags=["test-cases"])


def _check_cycle(db: Session, cycle_id: int) -> None:
    if not db.get(Cycle, cycle_id):
        raise HTTPException(404, "Cycle not found")


def _apply_review(tc: TestCase, body: ReviewBody, user: User) -> None:
    tc.status = body.status
    tc.review_notes = body.review_notes
    tc.reviewed_by = user.email if body.status != "pending" else None
    tc.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None) if body.status != "pending" else None
    tc.automation_status = "ready" if body.status == "approved" else "not_started"


@router.get("/test-cases")
def get_cases(cycle_id: int, test_type: str | None = None, status: str | None = None, q: str | None = None, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    _check_cycle(db, cycle_id)
    return list_cases(db, cycle_id, test_type, status, q)


@router.get("/test-cases/{tc_id}")
def get_case(cycle_id: int, tc_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    tc = db.scalar(
        select(TestCase)
        .where(TestCase.id == tc_id, TestCase.cycle_id == cycle_id)
        .options(selectinload(TestCase.steps), selectinload(TestCase.test_data), selectinload(TestCase.links))
    )
    if not tc:
        raise HTTPException(404, "Test case not found")
    return tc_to_dict(tc, _load_req_map(db, cycle_id))


@router.patch("/test-cases/{tc_id}")
def review_case(cycle_id: int, tc_id: int, body: ReviewBody, db: Session = Depends(get_db), user: User = Depends(require_role("tester"))):
    tc = db.get(TestCase, tc_id)
    if not tc or tc.cycle_id != cycle_id:
        raise HTTPException(404, "Test case not found")
    _apply_review(tc, body, user)
    db.add(AuditLog(cycle_id=cycle_id, actor=user.email, action=f"testcase.{body.status}", entity="test_case", entity_id=tc.tc_code, details={"notes": body.review_notes}))
    db.commit()
    return get_case(cycle_id, tc_id, db, user)


@router.put("/test-cases/{tc_id}")
def edit_case(cycle_id: int, tc_id: int, body: CaseEdit, db: Session = Depends(get_db), user: User = Depends(require_role("tester"))):
    """A reviewer corrects a case. Corrected cases become the strongest examples for later generations."""
    tc = db.scalar(select(TestCase).where(TestCase.id == tc_id, TestCase.cycle_id == cycle_id).options(selectinload(TestCase.steps)))
    if not tc:
        raise HTTPException(404, "Test case not found")
    changed = []
    if body.title is not None and body.title.strip() != tc.title:
        tc.title, changed = body.title.strip(), changed + ["title"]
    if body.priority is not None and body.priority != tc.priority:
        tc.priority, changed = body.priority, changed + ["priority"]
    if body.preconditions is not None:
        tc.preconditions = [p.strip() for p in body.preconditions if p.strip()]
        changed.append("preconditions")
    if body.steps is not None:
        # replacing the collection lets delete-orphan remove the old rows and keeps the loaded state correct
        tc.steps = [TCStep(seq=seq, action=s["action"], expected=s["expected"]) for seq, s in enumerate(body.steps, start=1)]
        changed.append("steps")
    if changed:
        tc.edited = True
        tc.warnings = []  # a person has now read and corrected it
        db.add(AuditLog(cycle_id=cycle_id, actor=user.email, action="testcase.edit", entity="test_case", entity_id=tc.tc_code, details={"fields": changed}))
        db.commit()
    return get_case(cycle_id, tc_id, db, user)


@router.patch("/test-cases")
def bulk_review(cycle_id: int, body: BulkReviewBody, db: Session = Depends(get_db), user: User = Depends(require_role("tester"))):
    cases = db.scalars(select(TestCase).where(TestCase.cycle_id == cycle_id, TestCase.id.in_(body.tc_ids))).all()
    if len(cases) != len(set(body.tc_ids)):
        raise HTTPException(404, "One or more test cases were not found in this cycle")
    for tc in cases:
        _apply_review(tc, body, user)
    db.add(AuditLog(cycle_id=cycle_id, actor=user.email, action=f"testcase.bulk_{body.status}", entity="cycle", entity_id=str(cycle_id), details={"count": len(cases)}))
    db.commit()
    return {"updated": len(cases), "status": body.status}


@router.get("/traceability")
def traceability(cycle_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    _check_cycle(db, cycle_id)
    return traceability_matrix(db, cycle_id)


@router.get("/export/csv")
def export(cycle_id: int, status: str | None = None, db: Session = Depends(get_db), _: User = Depends(require_role("qa_lead"))):
    _check_cycle(db, cycle_id)
    return Response(
        export_csv(db, cycle_id, status),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="cycle-{cycle_id}-test-cases.csv"'},
    )
