from __future__ import annotations

import csv
import io
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..models import GenerationRun, Requirement, TestCase, Traceability

PRIORITY_NUMBER = {"critical": 1, "high": 2, "medium": 3, "low": 4}


def req_to_dict(req: Requirement, stale_cases: int = 0, case_count: int = 0) -> dict:
    return {
        "id": req.id,
        "cycle_id": req.cycle_id,
        "code": req.code,
        "title": req.title,
        "description": req.description,
        "req_type": req.req_type,
        "acceptance_criteria": req.acceptance_criteria or [],
        "version": req.version,
        "updated_at": req.updated_at.isoformat() if req.updated_at else None,
        "test_case_count": case_count,
        "stale_test_cases": stale_cases,
    }


def _load_req_map(db: Session, cycle_id: int) -> dict[int, Requirement]:
    return {r.id: r for r in db.scalars(select(Requirement).where(Requirement.cycle_id == cycle_id))}


def tc_to_dict(tc: TestCase, reqs: dict[int, Requirement], detail: bool = True) -> dict:
    links = []
    stale = False
    for link in tc.links:
        req = reqs.get(link.requirement_id)
        if not req:
            continue
        is_stale = link.req_version < req.version
        stale = stale or is_stale
        links.append(
            {
                "code": req.code,
                "title": req.title,
                "written_against": link.req_version,
                "current_version": req.version,
                "stale": is_stale,
            }
        )
    data = {
        "id": tc.id,
        "tc_code": tc.tc_code,
        "title": tc.title,
        "tag": tc.tag,
        "test_type": tc.test_type,
        "subtype": tc.subtype,
        "priority": tc.priority,
        "status": tc.status,
        "automation_status": tc.automation_status,
        "stale": stale,
        "req_links": links,
        "reviewed_by": tc.reviewed_by,
        "reviewed_at": tc.reviewed_at.isoformat() if tc.reviewed_at else None,
        "review_notes": tc.review_notes,
        "step_count": len(tc.steps),
        "evidence": tc.evidence or "",
        "warnings": tc.warnings or [],
        "source": tc.source or "rules",
        "edited": bool(tc.edited),
    }
    if detail:
        data["preconditions"] = tc.preconditions or []
        data["steps"] = [{"seq": s.seq, "action": s.action, "expected": s.expected} for s in tc.steps]
        data["test_data"] = [
            {"field_name": d.field_name, "value": d.value, "step_seq": d.step_seq} for d in tc.test_data
        ]
    return data


def list_cases(db: Session, cycle_id: int, test_type: str | None = None, status: str | None = None, query: str | None = None, detail: bool = False) -> list[dict]:
    stmt = (
        select(TestCase)
        .where(TestCase.cycle_id == cycle_id)
        .options(selectinload(TestCase.steps), selectinload(TestCase.test_data), selectinload(TestCase.links))
        .order_by(TestCase.tc_code)
    )
    if test_type:
        stmt = stmt.where(TestCase.test_type == test_type)
    if status:
        stmt = stmt.where(TestCase.status == status)
    if query:
        stmt = stmt.where(TestCase.title.ilike(f"%{query}%"))
    reqs = _load_req_map(db, cycle_id)
    return [tc_to_dict(tc, reqs, detail=detail) for tc in db.scalars(stmt)]


def requirement_stats(db: Session, cycle_id: int) -> dict[int, dict]:
    stats: dict[int, dict] = defaultdict(lambda: {"cases": set(), "stale": set()})
    reqs = _load_req_map(db, cycle_id)
    for link in db.scalars(select(Traceability).where(Traceability.cycle_id == cycle_id)):
        req = reqs.get(link.requirement_id)
        if not req:
            continue
        stats[req.id]["cases"].add(link.tc_id)
        if link.req_version < req.version:
            stats[req.id]["stale"].add(link.tc_id)
    return stats


def traceability_matrix(db: Session, cycle_id: int) -> dict:
    reqs = list(db.scalars(select(Requirement).where(Requirement.cycle_id == cycle_id).order_by(Requirement.code)))
    cases = {tc.id: tc for tc in db.scalars(select(TestCase).where(TestCase.cycle_id == cycle_id))}
    per_req: dict[int, list[Traceability]] = defaultdict(list)
    for link in db.scalars(select(Traceability).where(Traceability.cycle_id == cycle_id)):
        per_req[link.requirement_id].append(link)

    rows = []
    covered = 0
    for req in reqs:
        links = [l for l in per_req.get(req.id, []) if l.tc_id in cases]
        statuses = {"pending": 0, "approved": 0, "rejected": 0}
        by_type: dict[str, int] = defaultdict(int)
        stale = 0
        for link in links:
            tc = cases[link.tc_id]
            statuses[tc.status] = statuses.get(tc.status, 0) + 1
            by_type[tc.test_type] += 1
            if link.req_version < req.version:
                stale += 1
        if not links:
            state = "uncovered"
        elif statuses["approved"] > 0:
            state = "covered"
            covered += 1
        else:
            state = "pending_review"
        rows.append(
            {
                "code": req.code,
                "title": req.title,
                "req_type": req.req_type,
                "version": req.version,
                "total": len(links),
                "statuses": statuses,
                "by_type": dict(by_type),
                "stale": stale,
                "state": state,
                "tc_codes": sorted(cases[l.tc_id].tc_code for l in links),
            }
        )
    total = len(reqs)
    return {
        "rows": rows,
        "summary": {
            "requirements": total,
            "covered": covered,
            "coverage_pct": round(100 * covered / total) if total else 0,
            "uncovered": sum(1 for r in rows if r["state"] == "uncovered"),
            "pending_review": sum(1 for r in rows if r["state"] == "pending_review"),
        },
    }


def cycle_summary(db: Session, cycle_id: int) -> dict:
    status_counts = dict(
        db.execute(select(TestCase.status, func.count()).where(TestCase.cycle_id == cycle_id).group_by(TestCase.status)).all()
    )
    type_counts = dict(
        db.execute(select(TestCase.test_type, func.count()).where(TestCase.cycle_id == cycle_id).group_by(TestCase.test_type)).all()
    )
    req_total = db.scalar(select(func.count()).select_from(Requirement).where(Requirement.cycle_id == cycle_id)) or 0
    stats = requirement_stats(db, cycle_id)
    stale_cases = set()
    for item in stats.values():
        stale_cases |= item["stale"]
    matrix = traceability_matrix(db, cycle_id)
    last_run = db.scalar(select(GenerationRun).where(GenerationRun.cycle_id == cycle_id).order_by(GenerationRun.id.desc()))
    return {
        "requirements": req_total,
        "test_cases": sum(status_counts.values()),
        "by_status": {s: status_counts.get(s, 0) for s in ("pending", "approved", "rejected")},
        "by_type": type_counts,
        "stale_test_cases": len(stale_cases),
        "stale_requirements": sum(1 for s in stats.values() if s["stale"]),
        "coverage_pct": matrix["summary"]["coverage_pct"],
        "uncovered_requirements": matrix["summary"]["uncovered"],
        "last_run": None
        if not last_run
        else {
            "id": last_run.id,
            "status": last_run.status,
            "mode": last_run.mode,
            "provider": last_run.provider,
            "started_at": last_run.started_at.isoformat(),
            "finished_at": last_run.finished_at.isoformat() if last_run.finished_at else None,
            "summary": last_run.summary,
            "error": last_run.error,
        },
    }


def export_csv(db: Session, cycle_id: int, status: str | None = None) -> str:
    stmt = (
        select(TestCase)
        .where(TestCase.cycle_id == cycle_id)
        .options(selectinload(TestCase.steps), selectinload(TestCase.links))
        .order_by(TestCase.tc_code)
    )
    if status:
        stmt = stmt.where(TestCase.status == status)
    reqs = _load_req_map(db, cycle_id)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        ["ID", "Work Item Type", "Title", "Test Step", "Step Action", "Step Expected", "State", "Priority", "Tags", "Test Type", "Requirements", "Automation Status"]
    )
    for tc in db.scalars(stmt):
        codes = ", ".join(sorted({reqs[l.requirement_id].code for l in tc.links if l.requirement_id in reqs}))
        writer.writerow(
            [tc.tc_code, "Test Case", tc.title, "", "", "", "Design" if tc.status != "approved" else "Ready",
             PRIORITY_NUMBER.get(tc.priority, 3), tc.tag, tc.test_type, codes, tc.automation_status]
        )
        for step in tc.steps:
            writer.writerow(["", "", "", step.seq, step.action, step.expected, "", "", "", "", "", ""])
    return out.getvalue()
