from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Cycle, User
from ..security import require_role
from ..services.scaffold import build_pytest

router = APIRouter(prefix="/api/cycles/{cycle_id}/export", tags=["automation"])


@router.get("/pytest")
def export_pytest(cycle_id: int, db: Session = Depends(get_db), _: User = Depends(require_role("qa_lead"))):
    """A pytest skeleton for the approved API cases. Assertions are left for a person to write."""
    if not db.get(Cycle, cycle_id):
        raise HTTPException(404, "Cycle not found")
    source, stats = build_pytest(db, cycle_id)
    if not stats["approved_api_cases"]:
        raise HTTPException(409, "There are no approved API test cases to export")
    return Response(
        source,
        media_type="text/x-python",
        headers={
            "Content-Disposition": f'attachment; filename="test_cycle_{cycle_id}_api.py"',
            "X-Trace-Written": str(stats["written"]),
            "X-Trace-Skipped": str(len(stats["skipped"])),
        },
    )
