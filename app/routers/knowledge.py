"""Project knowledge: the facts a generator must respect.

Three kinds of note, all injected into the prompt of the model-backed generators:
  glossary  what a term means in this product
  rule      a business rule or constraint that is true for this product
  avoid     something generated cases must never assume or do
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AuditLog, KnowledgeItem, Project, User
from ..schemas import KnowledgeCreate
from ..security import get_current_user, require_role

router = APIRouter(prefix="/api/projects/{project_id}/knowledge", tags=["knowledge"])


def _dict(item: KnowledgeItem) -> dict:
    return {
        "id": item.id,
        "kind": item.kind,
        "title": item.title,
        "content": item.content,
        "created_by": item.created_by,
        "created_at": item.created_at.isoformat(),
    }


def _project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


@router.get("")
def list_items(project_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    _project(db, project_id)
    return [_dict(i) for i in db.scalars(select(KnowledgeItem).where(KnowledgeItem.project_id == project_id).order_by(KnowledgeItem.id))]


@router.post("", status_code=201)
def add_item(project_id: int, body: KnowledgeCreate, db: Session = Depends(get_db), user: User = Depends(require_role("qa_lead"))):
    _project(db, project_id)
    item = KnowledgeItem(project_id=project_id, kind=body.kind, title=body.title.strip(), content=body.content.strip(), created_by=user.email)
    db.add(item)
    db.add(AuditLog(actor=user.email, action="knowledge.add", entity="project", entity_id=str(project_id), details={"kind": body.kind, "title": item.title}))
    db.commit()
    return _dict(item)


@router.delete("/{item_id}", status_code=204)
def delete_item(project_id: int, item_id: int, db: Session = Depends(get_db), user: User = Depends(require_role("qa_lead"))):
    item = db.get(KnowledgeItem, item_id)
    if not item or item.project_id != project_id:
        raise HTTPException(404, "Knowledge item not found")
    db.add(AuditLog(actor=user.email, action="knowledge.delete", entity="project", entity_id=str(project_id), details={"title": item.title}))
    db.delete(item)
    db.commit()
