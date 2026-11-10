"""Test runs: record what happened when approved cases were executed, and say how ready each requirement is."""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Requirement, TestCase, TestResult, TestRun, Traceability, utcnow

STATUSES = ("not_run", "passed", "failed", "blocked", "skipped")
PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class NoApprovedCases(Exception):
    pass


def create_run(db: Session, cycle_id: int, name: str, user_email: str) -> TestRun:
    """Snapshot the approved cases of the cycle into a new open run."""
    approved = db.scalars(
        select(TestCase.id).where(TestCase.cycle_id == cycle_id, TestCase.status == "approved").order_by(TestCase.tc_code)
    ).all()
    if not approved:
        raise NoApprovedCases()
    run = TestRun(cycle_id=cycle_id, name=name.strip(), created_by=user_email)
    run.results = [TestResult(tc_id=tc_id) for tc_id in approved]
    db.add(run)
    db.flush()
    return run


def _counts(statuses) -> dict:
    counts = {s: 0 for s in STATUSES}
    for status in statuses:
        counts[status] = counts.get(status, 0) + 1
    total = sum(counts.values())
    executed = total - counts["not_run"]
    return {
        **counts,
        "total": total,
        "executed": executed,
        "progress_pct": round(100 * executed / total) if total else 0,
        # skipped cases were deliberately not run, so they do not count for or against the rate
        "pass_rate": round(100 * counts["passed"] / (counts["passed"] + counts["failed"] + counts["blocked"]))
        if (counts["passed"] + counts["failed"] + counts["blocked"])
        else None,
    }


def run_summary(run: TestRun) -> dict:
    return {
        "id": run.id,
        "name": run.name,
        "status": run.status,
        "created_by": run.created_by,
        "created_at": run.created_at.isoformat(),
        "closed_at": run.closed_at.isoformat() if run.closed_at else None,
        **_counts(r.status for r in run.results),
    }


def run_detail(db: Session, run: TestRun) -> dict:
    cases = {tc.id: tc for tc in db.scalars(select(TestCase).where(TestCase.id.in_([r.tc_id for r in run.results])))}
    links: dict[int, list[Traceability]] = defaultdict(list)
    for link in db.scalars(select(Traceability).where(Traceability.tc_id.in_(list(cases)))):
        links[link.tc_id].append(link)
    reqs = {r.id: r for r in db.scalars(select(Requirement).where(Requirement.cycle_id == run.cycle_id))}

    rows = []
    by_type: dict[str, dict] = defaultdict(lambda: {s: 0 for s in STATUSES})
    by_priority: dict[str, dict] = defaultdict(lambda: {s: 0 for s in STATUSES})
    for result in run.results:
        tc = cases.get(result.tc_id)
        if not tc:
            continue
        linked = [reqs[l.requirement_id] for l in links.get(tc.id, []) if l.requirement_id in reqs]
        # the requirement changed after this run began, so the result may describe an older meaning
        stale = any(r.updated_at and r.updated_at > run.created_at for r in linked)
        by_type[tc.test_type][result.status] += 1
        by_priority[tc.priority][result.status] += 1
        rows.append(
            {
                "tc_id": tc.id,
                "tc_code": tc.tc_code,
                "title": tc.title,
                "test_type": tc.test_type,
                "priority": tc.priority,
                "requirements": sorted(r.code for r in linked),
                "status": result.status,
                "notes": result.notes,
                "defect_ref": result.defect_ref,
                "executed_by": result.executed_by,
                "executed_at": result.executed_at.isoformat() if result.executed_at else None,
                "stale_in_run": stale,
            }
        )
    rows.sort(key=lambda r: r["tc_code"])
    failures = [r for r in rows if r["status"] in ("failed", "blocked")]
    failures.sort(key=lambda r: (r["status"] != "failed", PRIORITY_ORDER.get(r["priority"], 9), r["tc_code"]))
    return {
        **run_summary(run),
        "results": rows,
        "failures": failures,
        "by_type": dict(by_type),
        "by_priority": {p: by_priority[p] for p in sorted(by_priority, key=lambda p: PRIORITY_ORDER.get(p, 9))},
    }


def record_result(result: TestResult, status: str, notes: str, defect_ref: str, user_email: str) -> None:
    result.status = status
    result.notes = notes.strip()
    result.defect_ref = defect_ref.strip()
    if status == "not_run":
        result.executed_by, result.executed_at = None, None
    else:
        result.executed_by, result.executed_at = user_email, utcnow()


def close_run(run: TestRun) -> None:
    run.status = "closed"
    run.closed_at = utcnow()


def readiness(db: Session, cycle_id: int) -> dict:
    """For each requirement, what the latest run says about its approved cases."""
    reqs = list(db.scalars(select(Requirement).where(Requirement.cycle_id == cycle_id).order_by(Requirement.code)))
    approved = {tc.id for tc in db.scalars(select(TestCase).where(TestCase.cycle_id == cycle_id, TestCase.status == "approved"))}

    latest: dict[int, str] = {}  # tc_id -> status in the most recent run that contains it
    run_ids = db.scalars(select(TestRun.id).where(TestRun.cycle_id == cycle_id).order_by(TestRun.id)).all()
    if run_ids:
        for result in db.scalars(select(TestResult).where(TestResult.run_id.in_(run_ids)).order_by(TestResult.run_id)):
            latest[result.tc_id] = result.status  # later runs overwrite earlier ones

    per_req: dict[int, set[int]] = defaultdict(set)
    for link in db.scalars(select(Traceability).where(Traceability.cycle_id == cycle_id)):
        if link.tc_id in approved:
            per_req[link.requirement_id].add(link.tc_id)

    rows, counts = [], defaultdict(int)
    for req in reqs:
        statuses = [latest.get(tc_id, "not_run") for tc_id in per_req.get(req.id, ())]
        if not statuses:
            state = "no_approved_cases"
        elif "failed" in statuses:
            state = "failing"
        elif "blocked" in statuses:
            state = "blocked"
        elif "passed" in statuses and all(s in ("passed", "skipped") for s in statuses):
            state = "passing"
        else:
            state = "not_run"
        counts[state] += 1
        rows.append({"code": req.code, "title": req.title, "state": state, "cases": len(statuses), "failed": statuses.count("failed")})
    total = len(reqs)
    return {
        "rows": rows,
        "summary": {
            "requirements": total,
            "passing": counts["passing"],
            "failing": counts["failing"],
            "blocked": counts["blocked"],
            "not_run": counts["not_run"],
            "no_approved_cases": counts["no_approved_cases"],
            "ready_pct": round(100 * counts["passing"] / total) if total else 0,
        },
    }
