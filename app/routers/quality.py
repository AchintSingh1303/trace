from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Cycle, Requirement, User
from ..security import get_current_user
from ..services.quality import Subject, check_cycle

router = APIRouter(prefix="/api/cycles/{cycle_id}/quality", tags=["quality"])


def _subjects(db: Session, cycle_id: int) -> list[Subject]:
    if not db.get(Cycle, cycle_id):
        raise HTTPException(404, "Cycle not found")
    reqs = db.scalars(select(Requirement).where(Requirement.cycle_id == cycle_id).order_by(Requirement.code))
    return [Subject(r.code, r.title, r.description, r.req_type, list(r.acceptance_criteria or [])) for r in reqs]


@router.get("")
def cycle_quality(cycle_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    """Score every requirement in the cycle. Advice only; generation is never blocked by it."""
    return check_cycle(_subjects(db, cycle_id))
