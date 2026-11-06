"""Generation orchestrator.

POST /generate returns immediately; this module does the work in a background task:

    load context -> run selected agents in parallel (streaming each case)
                 -> persist in one transaction -> compute coverage gaps -> done

Two modes:
  full         replace every case of the selected test types in the cycle.
  incremental  only touch requirements that are new or changed since their
               cases were written; everything else keeps its cases and approvals.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, select

from ..config import settings
from ..db import SessionLocal
from ..events import bus
from ..models import (
    AuditLog,
    Cycle,
    GenerationRun,
    Project,
    Requirement,
    TCStep,
    TCTestData,
    TestCase,
    Traceability,
    utcnow,
)
from . import agents as A
from .llm import LLMConfig, LLMError, generate_with_llm

_tasks: set[asyncio.Task] = set()


@dataclass
class Context:
    project: A.ProjectView
    reqs: list[A.ReqView]
    incremental: bool
    new_count: int
    stale_count: int
    # (requirement id, test type) pairs that already have cases generated for them
    owned: set[tuple[int, str]]
    stale_ids: set[int]
    # pairs that must be rebuilt because a requirement they also cover has changed
    forced: set[tuple[int, str]]

    def is_target(self, req_id: int, agent: str) -> bool:
        if not self.incremental:
            return True
        return (req_id, agent) not in self.owned or req_id in self.stale_ids or (req_id, agent) in self.forced


def start_generation(
    cycle_id: int, run_id: int, selected: list[str], mode: str, max_tcs: int | None, actor: str, llm: LLMConfig | None = None
) -> None:
    task = asyncio.create_task(run_generation(cycle_id, run_id, selected, mode, max_tcs, actor, llm))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# --------------------------------------------------------------------------- loading


def load_context(cycle_id: int, mode: str) -> Context:
    with SessionLocal() as db:
        cycle = db.get(Cycle, cycle_id)
        project = db.get(Project, cycle.project_id)
        reqs = db.scalars(select(Requirement).where(Requirement.cycle_id == cycle_id).order_by(Requirement.code)).all()
        current = {r.id: r.version for r in reqs}

        rows = db.execute(
            select(Traceability.tc_id, Traceability.requirement_id, Traceability.req_version, Traceability.is_primary, TestCase.test_type)
            .join(TestCase, TestCase.id == Traceability.tc_id)
            .where(Traceability.cycle_id == cycle_id)
        ).all()

        linked_reqs: set[int] = set()
        stale_ids: set[int] = set()
        owned: set[tuple[int, str]] = set()
        owner_of: dict[int, tuple[int, str]] = {}
        for tc_id, req_id, req_version, primary, test_type in rows:
            linked_reqs.add(req_id)
            if req_version < current.get(req_id, 0):
                stale_ids.add(req_id)
            if primary:
                owned.add((req_id, test_type))
                owner_of[tc_id] = (req_id, test_type)
        # a case that merely "also covers" a changed requirement is rebuilt through its owner
        forced = {owner_of[tc_id] for tc_id, req_id, _, primary, _ in rows if not primary and req_id in stale_ids and tc_id in owner_of}

        views = [
            A.ReqView(r.id, r.code, r.title, r.description, r.req_type, list(r.acceptance_criteria or []), r.version)
            for r in reqs
        ]
        return Context(
            project=A.ProjectView(project.name, project.app_type, project.description),
            reqs=views,
            incremental=mode == "incremental",
            new_count=sum(1 for r in reqs if r.id not in linked_reqs),
            stale_count=len(stale_ids),
            owned=owned,
            stale_ids=stale_ids,
            forced=forced,
        )


# --------------------------------------------------------------------------- agents


async def _run_agent(
    cycle_id: int, agent: str, targets: list[A.ReqView], ctx: Context, cap: int | None, llm: LLMConfig | None
) -> tuple[str, list[A.Case]]:
    bus.publish(cycle_id, "agent_start", {"test_type": agent, "requirements": len(targets)})
    generator = llm.label if llm else "rules"
    if not targets:
        cases: list[A.Case] = []
    elif llm:
        try:
            cases = await asyncio.to_thread(generate_with_llm, agent, targets, ctx.project, llm)
        except Exception as exc:  # one bad call must not sink the run: fall back and say so
            reason = str(exc) if isinstance(exc, LLMError) else f"unexpected {exc.__class__.__name__}"
            bus.publish(
                cycle_id,
                "agent_warning",
                {"test_type": agent, "message": f"{llm.label} could not generate {agent} cases ({reason}). Used the built-in rules instead."},
            )
            generator = "rules (fallback)"
            cases = A.run_agent(agent, targets, ctx.reqs)
    else:
        cases = A.run_agent(agent, targets, ctx.reqs)
    cases = A.sort_and_cap(cases, cap)
    delay = settings.generation_delay_ms / 1000
    for case in cases:
        bus.publish(
            cycle_id,
            "tc_created",
            {
                "title": case.title,
                "test_type": agent,
                "subtype": case.subtype,
                "priority": case.priority,
                "req_codes": case.req_codes,
            },
        )
        if delay:
            await asyncio.sleep(delay)
    bus.publish(
        cycle_id,
        "agent_complete",
        {"test_type": agent, "total": len(cases), "subtypes": A.subtype_summary(cases), "generator": generator},
    )
    return agent, cases


# --------------------------------------------------------------------------- persistence


def _next_numbers(db, cycle_id: int) -> dict[str, int]:
    highest: dict[str, int] = defaultdict(int)
    for (code,) in db.execute(select(TestCase.tc_code).where(TestCase.cycle_id == cycle_id)):
        match = re.match(r"TC-([A-Z]+)-(\d+)$", code)
        if match:
            highest[match.group(1)] = max(highest[match.group(1)], int(match.group(2)))
    return highest


def persist(cycle_id: int, run_id: int, results: dict[str, list[A.Case]], selected: list[str], ctx: Context) -> dict:
    with SessionLocal() as db:
        try:
            reqs = {r.code: r for r in db.scalars(select(Requirement).where(Requirement.cycle_id == cycle_id))}

            if not ctx.incremental:
                doomed = db.execute(
                    select(TestCase.id, TestCase.status).where(TestCase.cycle_id == cycle_id, TestCase.test_type.in_(selected))
                ).all()
            else:
                # only cases owned by a requirement that is being regenerated for that test type
                owned_rows = db.execute(
                    select(TestCase.id, TestCase.status, TestCase.test_type, Traceability.requirement_id)
                    .join(Traceability, Traceability.tc_id == TestCase.id)
                    .where(TestCase.cycle_id == cycle_id, TestCase.test_type.in_(selected), Traceability.is_primary.is_(True))
                ).all()
                doomed = [(tc_id, status) for tc_id, status, test_type, req_id in owned_rows if ctx.is_target(req_id, test_type)]
            doomed_ids = [row[0] for row in doomed]
            replaced_approved = sum(1 for row in doomed if row[1] == "approved")

            # traceability first: explicit, mirrors schemas where the link has no cascade
            for start in range(0, len(doomed_ids), 500):
                chunk = doomed_ids[start : start + 500]
                db.execute(delete(Traceability).where(Traceability.tc_id.in_(chunk)))
                db.execute(delete(TestCase).where(TestCase.id.in_(chunk)))
            db.flush()

            numbers = _next_numbers(db, cycle_id)
            by_type: dict[str, int] = {}
            for agent in selected:
                prefix = A.CODE_PREFIX[agent]
                for case in results.get(agent, []):
                    numbers[prefix] += 1
                    tc = TestCase(
                        cycle_id=cycle_id,
                        run_id=run_id,
                        tc_code=f"TC-{prefix}-{numbers[prefix]:03d}",
                        title=case.title,
                        tag=case.tag,
                        test_type=agent,
                        subtype=case.subtype,
                        priority=case.priority,
                        status="pending",
                        preconditions=case.preconditions,
                        automation_status="not_started",
                    )
                    db.add(tc)
                    db.flush()
                    for seq, step in enumerate(case.steps, start=1):
                        db.add(TCStep(tc_id=tc.id, seq=seq, action=step["action"], expected=step["expected"]))
                    for item in case.test_data:
                        db.add(TCTestData(tc_id=tc.id, field_name=item["field_name"], value=item["value"], step_seq=item.get("step_seq")))
                    linked = [reqs[c] for c in dict.fromkeys(case.req_codes) if c in reqs]
                    for position, req in enumerate(linked):
                        db.add(
                            Traceability(
                                cycle_id=cycle_id,
                                requirement_id=req.id,
                                tc_id=tc.id,
                                req_version=req.version,
                                is_primary=position == 0,
                            )
                        )
                    by_type[agent] = by_type.get(agent, 0) + 1

            summary = {
                "total": sum(by_type.values()),
                "by_type": by_type,
                "deleted": len(doomed_ids),
                "replaced_approved": replaced_approved,
            }
            run = db.get(GenerationRun, run_id)
            run.summary = summary
            db.add(
                AuditLog(
                    cycle_id=cycle_id,
                    actor=run.started_by,
                    action="generation.persist",
                    entity="cycle",
                    entity_id=str(cycle_id),
                    details=summary | {"mode": run.mode, "types": selected},
                )
            )
            db.commit()
            return summary
        except Exception:
            db.rollback()
            raise


def compute_gaps(cycle_id: int, selected: list[str]) -> list[dict]:
    with SessionLocal() as db:
        reqs = db.scalars(select(Requirement).where(Requirement.cycle_id == cycle_id).order_by(Requirement.code)).all()
        covered = {
            row[0]
            for row in db.execute(select(Traceability.requirement_id).where(Traceability.cycle_id == cycle_id)).all()
        }
        gaps = []
        for req in reqs:
            if req.id in covered:
                continue
            owners = [a for a, types in A.AGENT_INPUTS.items() if req.req_type in types]
            active = [a for a in owners if a in selected]
            gaps.append(
                {
                    "code": req.code,
                    "title": req.title,
                    "reason": "no_test_cases" if active else "agent_not_selected",
                    "missing_agents": [] if active else owners,
                }
            )
        return gaps


def _finish_run(run_id: int, status: str, error: str | None = None) -> None:
    with SessionLocal() as db:
        run = db.get(GenerationRun, run_id)
        if run:
            run.status = status
            run.finished_at = utcnow()
            run.error = error
            db.commit()


# --------------------------------------------------------------------------- orchestrator


async def run_generation(
    cycle_id: int, run_id: int, selected: list[str], mode: str, max_tcs: int | None, actor: str, llm: LLMConfig | None = None
) -> None:
    started = time.time()
    try:
        ctx = await asyncio.to_thread(load_context, cycle_id, mode)
        bus.publish(
            cycle_id,
            "pipeline_start",
            {
                "app": ctx.project.name,
                "mode": mode,
                "selected_types": selected,
                "provider": llm.provider if llm else "rules",
                "generator": llm.label if llm else "Built-in rules",
                "model": llm.model if llm else None,
                "requirements": len(ctx.reqs),
                "new_requirements": ctx.new_count,
                "stale_requirements": ctx.stale_count,
            },
        )
        caps = A.split_cap(max_tcs, selected)
        jobs = []
        for agent in selected:
            targets = [r for r in A.partition(agent, ctx.reqs) if ctx.is_target(r.id, agent)]
            jobs.append(_run_agent(cycle_id, agent, targets, ctx, caps[agent], llm))
        finished = await asyncio.gather(*jobs)
        results = dict(finished)

        summary = await asyncio.to_thread(persist, cycle_id, run_id, results, selected, ctx)
        gaps = await asyncio.to_thread(compute_gaps, cycle_id, selected)
        await asyncio.to_thread(_finish_run, run_id, "complete")
        bus.publish(
            cycle_id,
            "generation_complete",
            summary
            | {
                "elapsed_s": round(time.time() - started, 2),
                "coverage_gaps": gaps,
                "coverage_gap_count": len(gaps),
                "mode": mode,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as exc:
        message = str(exc) if isinstance(exc, LLMError) else f"{exc.__class__.__name__}: {exc}"
        if llm and llm.api_key:
            message = message.replace(llm.api_key, "[your key]")
        await asyncio.to_thread(_finish_run, run_id, "failed", message)
        bus.publish(cycle_id, "generation_error", {"error": message})
