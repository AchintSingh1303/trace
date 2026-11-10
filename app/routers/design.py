from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AuditLog, Cycle, DesignElement, User
from ..schemas import DesignBulkReview, DesignEdit, DesignGenerate, DesignReview
from ..security import get_current_user, require_role
from ..services import design as svc
from ..services.llm import resolve_config

router = APIRouter(prefix="/api/cycles/{cycle_id}/design", tags=["design"])


def _check_cycle(db: Session, cycle_id: int) -> None:
    if not db.get(Cycle, cycle_id):
        raise HTTPException(404, "Cycle not found")


def _element(db: Session, cycle_id: int, element_id: int) -> DesignElement:
    element = db.get(DesignElement, element_id)
    if not element or element.cycle_id != cycle_id:
        raise HTTPException(404, "Design element not found")
    return element


def _one(db: Session, cycle_id: int, element_id: int) -> dict:
    data = svc.list_design(db, cycle_id)
    return next(e for e in data["elements"] if e["id"] == element_id)


@router.get("")
def get_design(cycle_id: int, kind: str | None = None, status: str | None = None, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    _check_cycle(db, cycle_id)
    return svc.list_design(db, cycle_id, kind, status)


@router.post("/generate")
def generate(cycle_id: int, body: DesignGenerate, db: Session = Depends(get_db), user: User = Depends(require_role("qa_lead"))):
    _check_cycle(db, cycle_id)
    try:
        cfg = resolve_config(body.provider, body.api_key, body.model)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    try:
        return svc.draft(db, cycle_id, user.email, cfg)
    except svc.DesignBusy:
        raise HTTPException(409, "A design draft is already running for this cycle") from None
    except svc.NoRequirements:
        raise HTTPException(409, "Add at least one requirement before drafting a design") from None


@router.patch("")
def bulk_review(cycle_id: int, body: DesignBulkReview, db: Session = Depends(get_db), user: User = Depends(require_role("tester"))):
    found = db.scalars(select(DesignElement).where(DesignElement.cycle_id == cycle_id, DesignElement.id.in_(body.ids))).all()
    if len(found) != len(set(body.ids)):
        raise HTTPException(404, "One or more design elements were not found in this cycle")
    for element in found:
        svc.apply_review(element, body.status, body.review_notes, user.email)
    db.add(AuditLog(cycle_id=cycle_id, actor=user.email, action=f"design.bulk_{body.status}", entity="cycle", entity_id=str(cycle_id), details={"count": len(found)}))
    db.commit()
    return {"updated": len(found), "status": body.status}


@router.get("/export/md")
def export(cycle_id: int, db: Session = Depends(get_db), _: User = Depends(require_role("qa_lead"))):
    _check_cycle(db, cycle_id)
    text = svc.export_markdown(db, cycle_id)
    if text is None:
        raise HTTPException(409, "Approve at least one design element before exporting")
    return Response(text, media_type="text/markdown", headers={"Content-Disposition": f'attachment; filename="cycle-{cycle_id}-design.md"'})


@router.patch("/{element_id}")
def review(cycle_id: int, element_id: int, body: DesignReview, db: Session = Depends(get_db), user: User = Depends(require_role("tester"))):
    element = _element(db, cycle_id, element_id)
    svc.apply_review(element, body.status, body.review_notes, user.email)
    db.add(AuditLog(cycle_id=cycle_id, actor=user.email, action=f"design.{body.status}", entity="design_element", entity_id=element.code, details={"notes": body.review_notes}))
    db.commit()
    return _one(db, cycle_id, element_id)


@router.put("/{element_id}")
def edit(cycle_id: int, element_id: int, body: DesignEdit, db: Session = Depends(get_db), user: User = Depends(require_role("tester"))):
    element = _element(db, cycle_id, element_id)
    changed = svc.apply_edit(element, body.title, body.summary, body.detail)
    if changed:
        db.add(AuditLog(cycle_id=cycle_id, actor=user.email, action="design.edit", entity="design_element", entity_id=element.code, details={"fields": changed}))
        db.commit()
    return _one(db, cycle_id, element_id)
