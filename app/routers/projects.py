from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AuditLog, Cycle, Project, Requirement, TestCase, User
from ..schemas import CycleCreate, ProjectCreate
from ..security import get_current_user, require_role
from ..services.reporting import cycle_summary

router = APIRouter(prefix="/api", tags=["projects"])


def project_dict(db: Session, project: Project) -> dict:
    cycles = db.scalars(select(Cycle).where(Cycle.project_id == project.id).order_by(Cycle.id)).all()
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "app_type": project.app_type,
        "cycles": [{"id": c.id, "name": c.name, "status": c.status} for c in cycles],
    }


@router.get("/projects")
def list_projects(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return [project_dict(db, p) for p in db.scalars(select(Project).order_by(Project.id))]


@router.post("/projects", status_code=201)
def create_project(body: ProjectCreate, db: Session = Depends(get_db), user: User = Depends(require_role("qa_lead"))):
    project = Project(name=body.name.strip(), description=body.description, app_type=body.app_type)
    db.add(project)
    db.flush()
    cycle = Cycle(project_id=project.id, name="Cycle 1")
    db.add(cycle)
    db.add(AuditLog(actor=user.email, action="project.create", entity="project", entity_id=str(project.id), details={"name": project.name}))
    db.commit()
    return project_dict(db, project)


@router.post("/projects/{project_id}/cycles", status_code=201)
def create_cycle(project_id: int, body: CycleCreate, db: Session = Depends(get_db), user: User = Depends(require_role("qa_lead"))):
    if not db.get(Project, project_id):
        raise HTTPException(404, "Project not found")
    cycle = Cycle(project_id=project_id, name=body.name.strip())
    db.add(cycle)
    db.add(AuditLog(actor=user.email, action="cycle.create", entity="project", entity_id=str(project_id), details={"name": cycle.name}))
    db.commit()
    return {"id": cycle.id, "name": cycle.name, "status": cycle.status}


@router.get("/cycles/{cycle_id}")
def get_cycle(cycle_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    cycle = db.get(Cycle, cycle_id)
    if not cycle:
        raise HTTPException(404, "Cycle not found")
    project = db.get(Project, cycle.project_id)
    return {
        "id": cycle.id,
        "name": cycle.name,
        "status": cycle.status,
        "project": {"id": project.id, "name": project.name, "app_type": project.app_type},
        "summary": cycle_summary(db, cycle_id),
    }


@router.get("/cycles/{cycle_id}/audit")
def audit(cycle_id: int, limit: int = 40, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    rows = db.scalars(
        select(AuditLog).where(AuditLog.cycle_id == cycle_id).order_by(AuditLog.id.desc()).limit(min(limit, 200))
    ).all()
    return [
        {"id": r.id, "actor": r.actor, "action": r.action, "entity": r.entity, "entity_id": r.entity_id, "details": r.details, "at": r.created_at.isoformat()}
        for r in rows
    ]
