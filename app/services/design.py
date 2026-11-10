"""Design pipeline service: draft, keep what a person approved, save, list, review, export.

Drafting is synchronous. One call to the rules or to a model is short, so the request returns when the draft is
saved. Approved and human-edited elements survive a new draft; everything else is replaced.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from ..models import AuditLog, Cycle, DesignElement, DesignLink, KnowledgeItem, Project, Requirement
from . import agents as A
from .design_llm import draft_with_llm
from .design_rules import CODE_PREFIX, CLIENT, KINDS, Draft, Item, _near_duplicate, check_items, draft_rules, severity
from .llm import Guidance, LLMConfig, LLMError

_active: set[int] = set()
_guard = threading.Lock()

MAX_EXAMPLES = 8
MAX_AVOID = 6

EDITABLE_DETAIL = {
    "component": ("responsibility", "interfaces"),
    "flow": ("data", "trigger"),
    "risk": ("likelihood", "impact", "mitigation"),
}


class DesignBusy(Exception):
    pass


class NoRequirements(Exception):
    pass


# --------------------------------------------------------------------------- loading


def _views(db: Session, cycle_id: int) -> list[A.ReqView]:
    reqs = db.scalars(select(Requirement).where(Requirement.cycle_id == cycle_id).order_by(Requirement.code)).all()
    return [A.ReqView(r.id, r.code, r.title, r.description, r.req_type, list(r.acceptance_criteria or []), r.version) for r in reqs]


def load_guidance(db: Session, project_id: int) -> Guidance:
    knowledge = [
        {"kind": k.kind, "title": k.title, "content": k.content}
        for k in db.scalars(select(KnowledgeItem).where(KnowledgeItem.project_id == project_id).order_by(KnowledgeItem.id))
    ]
    cycle_ids = [c for (c,) in db.execute(select(Cycle.id).where(Cycle.project_id == project_id))]
    approved = db.scalars(
        select(DesignElement).where(DesignElement.cycle_id.in_(cycle_ids), DesignElement.status == "approved").order_by(DesignElement.id.desc()).limit(MAX_EXAMPLES)
    ).all()
    rejected = db.scalars(
        select(DesignElement)
        .where(DesignElement.cycle_id.in_(cycle_ids), DesignElement.status == "rejected", DesignElement.review_notes.is_not(None), DesignElement.review_notes != "")
        .order_by(DesignElement.id.desc())
        .limit(MAX_AVOID)
    ).all()
    return Guidance(
        knowledge=knowledge,
        examples=[{"kind": e.kind, "title": e.title, "summary": e.summary} for e in approved],
        avoid=[{"title": e.title, "reason": e.review_notes} for e in rejected],
    )


# --------------------------------------------------------------------------- drafting


def _next_numbers(db: Session, cycle_id: int) -> dict[str, int]:
    highest = {p: 0 for p in CODE_PREFIX.values()}
    for (code,) in db.execute(select(DesignElement.code).where(DesignElement.cycle_id == cycle_id)):
        match = re.match(r"([A-Z]+)-(\d+)$", code)
        if match and match.group(1) in highest:
            highest[match.group(1)] = max(highest[match.group(1)], int(match.group(2)))
    return highest


def draft(db: Session, cycle_id: int, actor: str, cfg: LLMConfig | None = None) -> dict:
    with _guard:
        if cycle_id in _active:
            raise DesignBusy()
        _active.add(cycle_id)
    try:
        return _draft(db, cycle_id, actor, cfg)
    finally:
        with _guard:
            _active.discard(cycle_id)


def _draft(db: Session, cycle_id: int, actor: str, cfg: LLMConfig | None) -> dict:
    cycle = db.get(Cycle, cycle_id)
    project = db.get(Project, cycle.project_id)
    reqs = _views(db, cycle_id)
    if not reqs:
        raise NoRequirements()
    req_rows = {r.code: r for r in db.scalars(select(Requirement).where(Requirement.cycle_id == cycle_id))}

    generator = cfg.label if cfg else "Built-in rules"
    fallback = None
    guidance = load_guidance(db, project.id)
    if cfg:
        try:
            result = draft_with_llm(reqs, A.ProjectView(project.name, project.app_type, project.description), cfg, guidance)
        except Exception as exc:  # one bad call must not lose the user's work: fall back and say so
            fallback = str(exc) if isinstance(exc, LLMError) else f"unexpected {exc.__class__.__name__}"
            if cfg.api_key:
                fallback = fallback.replace(cfg.api_key, "[your key]")
            result = draft_rules(reqs)
            for item in result.items:
                item.source = "rules-fallback"
            generator = "Built-in rules (fallback)"
    else:
        result = draft_rules(reqs)

    numbers = _next_numbers(db, cycle_id)  # taken before anything is removed, so a code is never reused
    existing = db.scalars(select(DesignElement).where(DesignElement.cycle_id == cycle_id).options(selectinload(DesignElement.links))).all()
    keep = [e for e in existing if e.status == "approved" or e.edited]
    doomed = [e for e in existing if e not in keep]
    replaced_ids = [e.id for e in doomed]

    kept_components = {e.title: e.code for e in keep if e.kind == "component"}
    knowledge_text = guidance.knowledge_text()
    items, report = check_items(result.items, reqs, knowledge_text, set(kept_components))

    # something a person already approved or corrected is not proposed again
    fresh: list[Item] = []
    skipped = 0
    for item in items:
        if any(e.kind == item.kind and _near_duplicate(e.title, item.title) for e in keep):
            skipped += 1
            continue
        fresh.append(item)

    for start in range(0, len(replaced_ids), 500):
        chunk = replaced_ids[start : start + 500]
        db.execute(delete(DesignLink).where(DesignLink.element_id.in_(chunk)))
        db.execute(delete(DesignElement).where(DesignElement.id.in_(chunk)))
    db.flush()

    code_by_name = dict(kept_components)
    created: list[tuple[Item, DesignElement]] = []
    for kind in KINDS:  # components first so flows and dependencies can point at their codes
        for item in (i for i in fresh if i.kind == kind):
            prefix = CODE_PREFIX[kind]
            numbers[prefix] += 1
            element = DesignElement(
                cycle_id=cycle_id,
                kind=kind,
                code=f"{prefix}-{numbers[prefix]:03d}",
                title=item.title[:300],
                summary=item.summary,
                detail={},
                evidence=item.evidence[:1000],
                status="pending",
                source=item.source,
                warnings=item.warnings,
            )
            db.add(element)
            db.flush()
            if kind == "component":
                code_by_name[item.title] = element.code
            created.append((item, element))

    for item, element in created:
        detail = dict(item.detail)
        if element.kind == "component":
            detail["depends_on"] = [code_by_name[n] for n in detail.get("depends_on", []) if n in code_by_name]
        elif element.kind == "flow":
            src, dst = detail.get("from_name", ""), detail.get("to_name", "")
            detail["from_code"] = code_by_name.get(src)
            detail["to_code"] = None if detail.get("to_external") else code_by_name.get(dst)
            if detail["from_code"]:
                detail["from_name"] = ""
            if detail["to_code"]:
                detail["to_name"] = ""
        elif element.kind == "risk":
            detail["severity"] = severity(detail.get("likelihood", "medium"), detail.get("impact", "medium"))
        element.detail = detail
        for code in dict.fromkeys(item.req_codes):
            req = req_rows.get(code)
            if req:
                db.add(DesignLink(cycle_id=cycle_id, element_id=element.id, requirement_id=req.id, req_version=req.version))

    by_kind = {k: sum(1 for i, _ in created if i.kind == k) for k in KINDS}
    summary = {
        "generator": generator,
        "created": by_kind,
        "total": len(created),
        "kept": len(keep),
        "replaced": len(replaced_ids),
        "already_approved": skipped,
        "dropped": len(report.dropped),
        "dropped_reasons": report.reasons(),
        "clarifications": result.clarifications,
        "fallback_reason": fallback,
    }
    db.add(AuditLog(cycle_id=cycle_id, actor=actor, action="design.draft", entity="cycle", entity_id=str(cycle_id), details={k: v for k, v in summary.items() if k != "clarifications"}))
    db.commit()
    return summary


# --------------------------------------------------------------------------- reading


def _load(db: Session, cycle_id: int) -> list[DesignElement]:
    return list(
        db.scalars(select(DesignElement).where(DesignElement.cycle_id == cycle_id).options(selectinload(DesignElement.links)).order_by(DesignElement.code))
    )


def element_to_dict(e: DesignElement, reqs: dict[int, Requirement], names: dict[str, str]) -> dict:
    links, stale = [], False
    for link in e.links:
        req = reqs.get(link.requirement_id)
        if not req:
            continue
        is_stale = link.req_version < req.version
        stale = stale or is_stale
        links.append({"code": req.code, "title": req.title, "written_against": link.req_version, "current_version": req.version, "stale": is_stale})
    detail = dict(e.detail or {})
    if e.kind == "component":
        detail["depends_on_names"] = [{"code": c, "name": names.get(c, c)} for c in detail.get("depends_on", [])]
    elif e.kind == "flow":
        detail["from_label"] = names.get(detail.get("from_code") or "", detail.get("from_name") or CLIENT)
        detail["to_label"] = names.get(detail.get("to_code") or "", detail.get("to_name") or "")
    return {
        "id": e.id,
        "code": e.code,
        "kind": e.kind,
        "title": e.title,
        "summary": e.summary,
        "detail": detail,
        "evidence": e.evidence or "",
        "status": e.status,
        "review_notes": e.review_notes,
        "reviewed_by": e.reviewed_by,
        "reviewed_at": e.reviewed_at.isoformat() if e.reviewed_at else None,
        "edited": bool(e.edited),
        "source": e.source or "rules",
        "warnings": e.warnings or [],
        "stale": stale,
        "req_links": links,
    }


def list_design(db: Session, cycle_id: int, kind: str | None = None, status: str | None = None) -> dict:
    elements = _load(db, cycle_id)
    reqs = {r.id: r for r in db.scalars(select(Requirement).where(Requirement.cycle_id == cycle_id).order_by(Requirement.code))}
    names = {e.code: e.title for e in elements if e.kind == "component"}
    rows = [element_to_dict(e, reqs, names) for e in elements]
    cited: set[int] = set()
    for e in elements:
        if e.status != "rejected":
            cited.update(link.requirement_id for link in e.links)
    gaps = [{"code": r.code, "title": r.title} for r in reqs.values() if r.id not in cited]
    counts = {k: {s: 0 for s in ("pending", "approved", "rejected")} for k in KINDS}
    for e in elements:
        counts[e.kind][e.status] = counts[e.kind].get(e.status, 0) + 1
    shown = [r for r in rows if (not kind or r["kind"] == kind) and (not status or r["status"] == status)]
    return {
        "elements": shown,
        "summary": {
            "total": len(rows),
            "counts": counts,
            "stale": sum(1 for r in rows if r["stale"]),
            "high_risks": sum(1 for r in rows if r["kind"] == "risk" and r["detail"].get("severity") == "high" and r["status"] != "rejected"),
            "requirements": len(reqs),
            "covered": len(reqs) - len(gaps),
            "gaps": gaps,
        },
    }


def design_by_requirement(db: Session, cycle_id: int) -> dict[str, list[str]]:
    """Codes of approved design elements per requirement code, for the traceability matrix."""
    reqs = {r.id: r.code for r in db.scalars(select(Requirement).where(Requirement.cycle_id == cycle_id))}
    out: dict[str, list[str]] = {}
    for e in _load(db, cycle_id):
        if e.status != "approved":
            continue
        for link in e.links:
            if link.requirement_id in reqs:
                out.setdefault(reqs[link.requirement_id], []).append(e.code)
    return {k: sorted(v) for k, v in out.items()}


# --------------------------------------------------------------------------- review and edit


def apply_review(e: DesignElement, status: str, notes: str | None, actor: str) -> None:
    e.status = status
    e.review_notes = notes
    e.reviewed_by = actor if status != "pending" else None
    e.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None) if status != "pending" else None


def apply_edit(e: DesignElement, title: str | None, summary: str | None, detail: dict | None) -> list[str]:
    changed: list[str] = []
    if title is not None and title.strip() and title.strip() != e.title:
        e.title, changed = title.strip(), changed + ["title"]
    if summary is not None and summary.strip() != e.summary:
        e.summary, changed = summary.strip(), changed + ["summary"]
    if detail:
        current = dict(e.detail or {})
        for key in EDITABLE_DETAIL[e.kind]:
            if key in detail:
                value = detail[key]
                value = [str(v).strip() for v in value if str(v).strip()] if key == "interfaces" else str(value).strip()
                if value != current.get(key):
                    current[key] = value
                    changed.append(key)
        if e.kind == "risk":
            current["severity"] = severity(current.get("likelihood", "medium"), current.get("impact", "medium"))
        e.detail = current
    if changed:
        e.edited = True
        e.warnings = []
    return changed


# --------------------------------------------------------------------------- export


def _cell(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).replace("|", "\\|").strip() or "-"


def _label(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).replace('"', "'").strip()


def _short(text: str, limit: int = 36) -> str:
    """Cut at a word boundary so a diagram label never ends mid-word."""
    text = _label(text)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:")
    return (cut or text[:limit]) + "..."


def _edge_label(title: str, data: str, from_name: str) -> str:
    """A flow titled 'A to B' repeats what the arrow already shows, so label it with the data instead."""
    if from_name and title.lower().startswith(from_name.lower()) and data:
        return _short(data)
    return _short(title)


def export_markdown(db: Session, cycle_id: int) -> str | None:
    """The approved design as a Markdown document. None when nothing is approved."""
    cycle = db.get(Cycle, cycle_id)
    project = db.get(Project, cycle.project_id)
    elements = [e for e in _load(db, cycle_id) if e.status == "approved"]
    if not elements:
        return None
    reqs = {r.id: r for r in db.scalars(select(Requirement).where(Requirement.cycle_id == cycle_id))}
    names = {e.code: e.title for e in elements if e.kind == "component"}

    def req_codes(e: DesignElement) -> str:
        return ", ".join(sorted(reqs[l.requirement_id].code for l in e.links if l.requirement_id in reqs))

    comps = [e for e in elements if e.kind == "component"]
    flows = [e for e in elements if e.kind == "flow"]
    risks = [e for e in elements if e.kind == "risk"]
    out = [f"# Design: {project.name} ({cycle.name})", "", "Approved elements only. Each one cites the requirements it came from.", ""]

    if comps:
        out += ["## Components", "", "| Code | Component | Responsibility | Interfaces | Depends on | Requirements |", "| --- | --- | --- | --- | --- | --- |"]
        for e in comps:
            d = e.detail or {}
            deps = ", ".join(names.get(c, c) for c in d.get("depends_on", []))
            out.append(f"| {e.code} | {_cell(e.title)} | {_cell(d.get('responsibility') or e.summary)} | {_cell(', '.join(d.get('interfaces', [])))} | {_cell(deps)} | {_cell(req_codes(e))} |")
        out.append("")

    if comps or flows:
        out += ["## Diagram", "", "```mermaid", "flowchart LR"]
        node_ids: dict[str, str] = {}

        def node(key: str, label: str, shape: str = "[{}]") -> str:
            if key not in node_ids:
                node_ids[key] = f"N{len(node_ids) + 1}"
                out.append("    " + node_ids[key] + shape.format(f'"{_label(label)}"'))
            return node_ids[key]

        for e in comps:
            node(e.code, e.title)
        for e in comps:
            for dep in (e.detail or {}).get("depends_on", []):
                if dep in names:
                    out.append(f"    {node(e.code, e.title)} -.-> {node(dep, names[dep])}")
        for e in flows:
            d = e.detail or {}
            src = node(d["from_code"], names[d["from_code"]]) if d.get("from_code") in names else node("ext:" + (d.get("from_name") or CLIENT), d.get("from_name") or CLIENT, "([{}])")
            dst = node(d["to_code"], names[d["to_code"]]) if d.get("to_code") in names else node("ext:" + (d.get("to_name") or "Other"), d.get("to_name") or "Other", "([{}])")
            from_name = names.get(d.get("from_code") or "", d.get("from_name") or CLIENT)
            out.append(f"    {src} -->|\"{_edge_label(e.title, d.get('data', ''), from_name)}\"| {dst}")
        out += ["```", ""]

    if flows:
        out += ["## Data flows", "", "| Code | Flow | From | To | Data | Trigger | Requirements |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for e in flows:
            d = e.detail or {}
            src = names.get(d.get("from_code") or "", d.get("from_name") or CLIENT)
            dst = names.get(d.get("to_code") or "", d.get("to_name") or "")
            out.append(f"| {e.code} | {_cell(e.title)} | {_cell(src)} | {_cell(dst)} | {_cell(d.get('data'))} | {_cell(d.get('trigger'))} | {_cell(req_codes(e))} |")
        out.append("")

    if risks:
        rank = {"high": 0, "medium": 1, "low": 2}
        out += ["## Risk register", "", "| Code | Risk | Severity | Likelihood | Impact | Mitigation | Requirements |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for e in sorted(risks, key=lambda r: (rank.get((r.detail or {}).get("severity", "medium"), 1), r.code)):
            d = e.detail or {}
            out.append(f"| {e.code} | {_cell(e.title)} | {_cell(d.get('severity'))} | {_cell(d.get('likelihood'))} | {_cell(d.get('impact'))} | {_cell(d.get('mitigation'))} | {_cell(req_codes(e))} |")
        out.append("")

    per_req: dict[str, list[str]] = {}
    for e in elements:
        for link in e.links:
            if link.requirement_id in reqs:
                per_req.setdefault(reqs[link.requirement_id].code, []).append(e.code)
    out += ["## Requirement to design", "", "| Requirement | Design elements |", "| --- | --- |"]
    for req in sorted(reqs.values(), key=lambda r: r.code):
        out.append(f"| {req.code} {_cell(req.title)} | {_cell(', '.join(sorted(per_req.get(req.code, []))) or 'not covered')} |")
    out.append("")
    return "\n".join(out)
