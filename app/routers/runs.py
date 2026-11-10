from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AuditLog, Cycle, TestResult, TestRun, User
from ..schemas import ResultUpdate, RunCreate
from ..security import get_current_user, require_role
from ..services import execution

router = APIRouter(prefix="/api/cycles/{cycle_id}", tags=["runs"])


def _check_cycle(db: Session, cycle_id: int) -> None:
    if not db.get(Cycle, cycle_id):
        raise HTTPException(404, "Cycle not found")


def _run_or_404(db: Session, cycle_id: int, run_id: int) -> TestRun:
    run = db.get(TestRun, run_id)
    if not run or run.cycle_id != cycle_id:
        raise HTTPException(404, "Run not found")
    return run


@router.get("/runs")
def list_runs(cycle_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    _check_cycle(db, cycle_id)
    runs = db.scalars(select(TestRun).where(TestRun.cycle_id == cycle_id).order_by(TestRun.id.desc())).all()
    return [execution.run_summary(r) for r in runs]


@router.post("/runs", status_code=201)
def create_run(cycle_id: int, body: RunCreate, db: Session = Depends(get_db), user: User = Depends(require_role("qa_lead"))):
    """Start a run over the cases that are approved right now."""
    _check_cycle(db, cycle_id)
    try:
        run = execution.create_run(db, cycle_id, body.name, user.email)
    except execution.NoApprovedCases:
        raise HTTPException(409, "There are no approved test cases to run. Approve some in the Review tab first.")
    db.add(AuditLog(cycle_id=cycle_id, actor=user.email, action="run.create", entity="run", entity_id=str(run.id), details={"name": run.name, "cases": len(run.results)}))
    db.commit()
    return execution.run_detail(db, run)


@router.get("/runs/{run_id}")
def get_run(cycle_id: int, run_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return execution.run_detail(db, _run_or_404(db, cycle_id, run_id))


@router.put("/runs/{run_id}/results/{tc_id}")
def record_result(cycle_id: int, run_id: int, tc_id: int, body: ResultUpdate, db: Session = Depends(get_db), user: User = Depends(require_role("tester"))):
    run = _run_or_404(db, cycle_id, run_id)
    if run.status != "open":
        raise HTTPException(409, "This run is closed, so its results can no longer change")
    result = db.scalar(select(TestResult).where(TestResult.run_id == run_id, TestResult.tc_id == tc_id))
    if not result:
        raise HTTPException(404, "That test case is not part of this run")
    execution.record_result(result, body.status, body.notes, body.defect_ref, user.email)
    db.add(AuditLog(cycle_id=cycle_id, actor=user.email, action=f"result.{body.status}", entity="run", entity_id=str(run_id), details={"tc_id": tc_id}))
    db.commit()
    return execution.run_summary(run)


@router.post("/runs/{run_id}/close")
def close_run(cycle_id: int, run_id: int, db: Session = Depends(get_db), user: User = Depends(require_role("qa_lead"))):
    run = _run_or_404(db, cycle_id, run_id)
    if run.status == "open":
        execution.close_run(run)
        db.add(AuditLog(cycle_id=cycle_id, actor=user.email, action="run.close", entity="run", entity_id=str(run_id), details={}))
        db.commit()
    return execution.run_summary(run)


@router.get("/readiness")
def readiness(cycle_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    _check_cycle(db, cycle_id)
    return execution.readiness(db, cycle_id)
