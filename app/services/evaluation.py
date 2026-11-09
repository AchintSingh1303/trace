"""Scores generated cases against a golden set, so a change to a prompt, a model or the rules can be measured.

For every golden requirement we check:
  criteria coverage  how many of its acceptance criteria are tested by at least one case
  must-cover         phrases a good answer has to include (limits, error codes, rules)
  forbidden          phrases that would be inventions; any hit is a hallucination
Across the run we also report how many cases the grounding check dropped or warned about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from . import agents as A
from .grounding import check_cases, content_tokens, normalise

# (agent, requirements, project) -> (cases, dropped, warned)
Generator = Callable[[str, list[A.ReqView], A.ProjectView], tuple[list[A.Case], int, int]]


@dataclass
class ReqScore:
    code: str
    cases: int
    criteria_covered: int
    criteria_total: int
    must_hit: int
    must_total: int
    forbidden_hits: list[str] = field(default_factory=list)


def _case_text(case: A.Case) -> str:
    parts = [case.title, *case.preconditions]
    for step in case.steps:
        parts += [step.get("action", ""), step.get("expected", "")]
    return normalise(" ".join(parts))


def _criterion_covered(criterion: str, texts: list[str]) -> bool:
    need = content_tokens(criterion)
    if not need:
        return True
    return any(len(need & set(t.split())) / len(need) >= 0.6 for t in texts)


def rules_generator(agent, targets, project):
    return A.run_agent(agent, targets, targets), 0, 0


def score(golden: dict, generate: Generator) -> dict:
    project = A.ProjectView(**{k: golden["project"].get(k, "") for k in ("name", "app_type", "description")})
    reqs = [
        A.ReqView(i, r["code"], r["title"], r.get("description", ""), r["type"], r.get("acceptance_criteria", []), 1)
        for i, r in enumerate(golden["requirements"], start=1)
    ]
    spec = {r["code"]: r for r in golden["requirements"]}
    by_agent: dict[str, list[A.ReqView]] = {}
    for req in reqs:
        by_agent.setdefault(spec[req.code]["agent"], []).append(req)

    all_cases: list[A.Case] = []
    dropped = warned = 0
    for agent, targets in by_agent.items():
        cases, d, w = generate(agent, targets, project)
        all_cases += cases
        dropped, warned = dropped + d, warned + w

    scores: list[ReqScore] = []
    for req in reqs:
        linked = [c for c in all_cases if req.code in c.req_codes]
        texts = [_case_text(c) for c in linked]
        blob = " ".join(texts)
        s = spec[req.code]
        must = s.get("must_cover", [])
        scores.append(
            ReqScore(
                code=req.code,
                cases=len(linked),
                criteria_covered=sum(_criterion_covered(c, texts) for c in req.acceptance_criteria),
                criteria_total=len(req.acceptance_criteria),
                must_hit=sum(normalise(m) in blob for m in must),
                must_total=len(must),
                forbidden_hits=[f for f in s.get("forbidden", []) if normalise(f) in blob],
            )
        )

    crit_total = sum(s.criteria_total for s in scores) or 1
    must_total = sum(s.must_total for s in scores) or 1
    produced = len(all_cases) + dropped
    result = {
        "requirements": [s.__dict__ for s in scores],
        "cases": len(all_cases),
        "criteria_coverage": round(sum(s.criteria_covered for s in scores) / crit_total, 3),
        "must_cover": round(sum(s.must_hit for s in scores) / must_total, 3),
        "forbidden_hits": sum(len(s.forbidden_hits) for s in scores),
        "dropped": dropped,
        "ungrounded_rate": round((dropped + warned) / produced, 3) if produced else 0.0,
    }
    limits = golden.get("thresholds", {})
    failures = []
    if result["criteria_coverage"] < limits.get("criteria_coverage", 0):
        failures.append(f"criteria coverage {result['criteria_coverage']} is below {limits['criteria_coverage']}")
    if result["must_cover"] < limits.get("must_cover", 0):
        failures.append(f"must-cover {result['must_cover']} is below {limits['must_cover']}")
    if result["forbidden_hits"] > limits.get("max_forbidden", 10**6):
        failures.append(f"{result['forbidden_hits']} forbidden (invented) phrase(s) appeared")
    if result["ungrounded_rate"] > limits.get("max_ungrounded_rate", 1):
        failures.append(f"ungrounded rate {result['ungrounded_rate']} is above {limits['max_ungrounded_rate']}")
    result["failures"] = failures
    result["passed"] = not failures
    return result


def llm_generator(cfg, strict: bool = False) -> Generator:
    from .llm import generate_with_llm

    def generate(agent, targets, project):
        res = generate_with_llm(agent, targets, project, cfg)
        kept, report = check_cases(res.cases, targets, project_text=f"{project.name} {project.description}", strict=strict)
        return kept, report.dropped_count, report.warned

    return generate
