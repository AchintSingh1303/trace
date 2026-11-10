"""Requirement quality check.

Deterministic rules that say whether a requirement is written well enough to be tested. No model is involved, so
the result is instant and identical every time. Findings are advice: nothing here blocks generation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .grounding import content_tokens

# The weights are a judgment call. Change them here if your team scores differently.
PENALTY = {"error": 25, "warning": 10, "info": 3}
GRADES = [(85, "A"), (70, "B"), (50, "C"), (0, "D")]
DUPLICATE_OVERLAP = 0.8
MIN_CRITERION_WORDS = 4

VAGUE_TERMS = [
    "fast", "quick", "quickly", "easy", "easily", "user-friendly", "user friendly", "intuitive", "simple",
    "robust", "flexible", "seamless", "seamlessly", "appropriate", "appropriately", "adequate", "sufficient",
    "efficient", "efficiently", "reasonable", "modern", "optimal", "as needed", "if necessary",
    "where possible", "as soon as possible", "and so on", "etc",
]
_VAGUE_RE = re.compile(r"(?<![\w-])(" + "|".join(re.escape(t) for t in VAGUE_TERMS) + r")(?![\w-])", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"(?<!\w)(tbd|tbc|todo|to be decided|to be confirmed)(?!\w)|\?\?\?", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d")
_ENDPOINT_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+/")
_CONJUNCTION_RE = re.compile(r"\b(and|or)\b", re.IGNORECASE)


@dataclass
class Finding:
    code: str
    severity: str
    field: str
    message: str
    suggestion: str

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "field": self.field,
            "message": self.message,
            "suggestion": self.suggestion,
        }


@dataclass
class Subject:
    """Just the parts of a requirement the rules read, so they work on a draft that is not saved yet."""

    code: str
    title: str
    description: str
    req_type: str
    acceptance_criteria: list[str] = field(default_factory=list)


def grade_for(score: int) -> str:
    for floor, grade in GRADES:
        if score >= floor:
            return grade
    return "D"


def _scan_vague(text: str, location: str, out: list[Finding]) -> None:
    seen = set()
    for match in _VAGUE_RE.finditer(text or ""):
        word = match.group(1).lower()
        if word in seen:
            continue
        seen.add(word)
        out.append(
            Finding(
                "VAGUE_TERM",
                "warning",
                location,
                f'"{match.group(1)}" cannot be checked as written.',
                "Say how it is measured, for example a number, a limit or an exact message.",
            )
        )


def check_requirement(req: Subject) -> list[Finding]:
    findings: list[Finding] = []
    criteria = [c for c in req.acceptance_criteria if c and c.strip()]
    everything = " ".join([req.title, req.description, *criteria])

    if not criteria:
        findings.append(
            Finding("NO_CRITERIA", "error", "acceptance_criteria", "There are no acceptance criteria.",
                    "Add at least one observable result that proves the requirement is met.")
        )
    elif len(criteria) == 1 and req.req_type in ("Functional", "UI"):
        findings.append(
            Finding("FEW_CRITERIA", "info", "acceptance_criteria", "Only one acceptance criterion.",
                    "Consider the failure and limit cases, not only the main path.")
        )

    if _PLACEHOLDER_RE.search(everything):
        findings.append(
            Finding("PLACEHOLDER", "error", "description", "A placeholder (TBD, TODO or similar) is still in the text.",
                    "Replace it with the decision, or remove the requirement until it is made.")
        )

    _scan_vague(req.title, "title", findings)
    _scan_vague(req.description, "description", findings)
    for n, text in enumerate(criteria):
        _scan_vague(text, f"acceptance_criteria[{n}]", findings)

    if req.req_type == "Performance" and not _NUMBER_RE.search(" ".join([req.description, *criteria])):
        findings.append(
            Finding("UNMEASURED_PERFORMANCE", "warning", "acceptance_criteria",
                    "A performance requirement has no number in it.",
                    "State a target such as a response time, a user count or an error rate.")
        )

    if req.req_type == "API" and not _ENDPOINT_RE.search(everything):
        findings.append(
            Finding("API_NO_ENDPOINT", "warning", "title", "An API requirement does not name a method and path.",
                    "Write it as METHOD /path, for example POST /orders.")
        )

    for n, text in enumerate(criteria):
        words = len(text.split())
        if words < MIN_CRITERION_WORDS:
            findings.append(
                Finding("SHORT_CRITERION", "warning", f"acceptance_criteria[{n}]",
                        f"Only {words} words, too short to verify on its own.",
                        "Say what is done and what must be seen afterwards.")
            )
        if len(_CONJUNCTION_RE.findall(text)) >= 2:
            findings.append(
                Finding("COMPOUND_CRITERION", "info", f"acceptance_criteria[{n}]",
                        "This criterion joins several checks.", "Split it so each check can pass or fail alone.")
            )

    if len((req.description or "").strip()) < 20:
        findings.append(
            Finding("THIN_DESCRIPTION", "info", "description", "The description is very short.",
                    "Add the context a tester would need: who, when and why.")
        )
    return findings


def score_findings(findings: list[Finding]) -> int:
    return max(0, 100 - sum(PENALTY[f.severity] for f in findings))


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def check_cycle(reqs: list[Subject]) -> dict:
    """Score every requirement and flag near-duplicates between them."""
    tokens = {r.code: content_tokens(f"{r.title} {r.description}") for r in reqs}
    results = []
    for req in reqs:
        findings = check_requirement(req)
        for other in reqs:
            if other.code != req.code and _overlap(tokens[req.code], tokens[other.code]) >= DUPLICATE_OVERLAP:
                findings.append(
                    Finding("NEAR_DUPLICATE", "info", "title", f"Reads almost the same as {other.code}.",
                            "Merge them, or make the difference explicit.")
                )
        score = score_findings(findings)
        results.append(
            {
                "code": req.code,
                "title": req.title,
                "score": score,
                "grade": grade_for(score),
                "findings": [f.as_dict() for f in findings],
            }
        )
    grades = {"A": 0, "B": 0, "C": 0, "D": 0}
    for r in results:
        grades[r["grade"]] += 1
    scores = [r["score"] for r in results]
    return {
        "requirements": results,
        "summary": {
            "count": len(results),
            "average_score": round(sum(scores) / len(scores)) if scores else 0,
            "grades": grades,
            "with_errors": sum(1 for r in results if any(f["severity"] == "error" for f in r["findings"])),
        },
    }
