from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..events import bus, sse_format
from ..models import Cycle, GenerationRun, Requirement, User
from ..schemas import GenerateBody
from ..security import get_current_user, require_role
from ..services.generation import start_generation
from ..services.llm import resolve_config

router = APIRouter(prefix="/api/cycles/{cycle_id}/generate", tags=["generation"])


@router.post("")
async def generate(cycle_id: int, body: GenerateBody, db: Session = Depends(get_db), user: User = Depends(require_role("qa_lead"))):
    if not db.get(Cycle, cycle_id):
        raise HTTPException(404, "Cycle not found")
    if bus.is_active(cycle_id):
        raise HTTPException(409, "Generation is already running for this cycle")
    if not db.scalar(select(func.count()).select_from(Requirement).where(Requirement.cycle_id == cycle_id)):
        raise HTTPException(409, "Add at least one requirement before generating test cases")

    try:
        llm_config = resolve_config(body.provider, body.api_key, body.model)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None

    # Only the provider name and model are recorded. The key stays in memory for this run.
    provider = llm_config.provider if llm_config else "rules"
    run = GenerationRun(
        cycle_id=cycle_id,
        mode=body.mode,
        selected_types=body.selected_test_types,
        provider=provider,
        started_by=user.email,
    )
    db.add(run)
    db.commit()

    bus.start(cycle_id)
    start_generation(cycle_id, run.id, body.selected_test_types, body.mode, body.max_tcs, user.email, llm_config)
    return {
        "cycle_id": cycle_id,
        "run_id": run.id,
        "status": "started",
        "provider": provider,
        "model": llm_config.model if llm_config else None,
    }


@router.get("/status")
def status(cycle_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    """Snapshot so a page that was closed mid-run can rebuild its progress view."""
    snap = bus.snapshot(cycle_id)
    last = db.scalar(select(GenerationRun).where(GenerationRun.cycle_id == cycle_id).order_by(GenerationRun.id.desc()))
    return {"active": snap["active"], "events": snap["events"], "last_run_status": last.status if last else None}


@router.get("/stream")
async def stream(cycle_id: int, _: User = Depends(get_current_user)):
    async def events():
        async for event in bus.subscribe(cycle_id):
            yield sse_format(event)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
