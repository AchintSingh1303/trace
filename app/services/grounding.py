"""Grounding checks for model-written test cases.

A model can write a fluent case about something the requirement never said. These checks
catch the common forms of that before a case reaches the review queue:

  * every case must quote the part of the requirement it verifies (`evidence`),
    and the quote has to really appear in the requirement text or project knowledge;
  * steps may not mention endpoints or field names that appear nowhere in the
    requirement, the project description or the project knowledge;
  * near-duplicate cases are collapsed.

Cases that fail the first check are dropped. The second check only attaches a warning
(or drops the case in strict mode), because a reviewer can often tell at a glance
whether a flagged term is a real invention or a sensible detail.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .agents import Case, ReqView

STOPWORDS = frozenset(
    "a an and are as at be by can for from has have if in into is it its of on or that the their then "
    "this to user users verify verifies that was were when which will with without not no".split()
)

_WORD = re.compile(r"[a-z0-9]+")
_PATH = re.compile(r"(?<![\w:/])(/[A-Za-z0-9_\-{}.]+(?:/[A-Za-z0-9_\-{}.]+)*)")
_IDENT = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b|\b[a-z]+(?:[A-Z][a-z0-9]+)+\b")


def normalise(text: str) -> str:
    return " ".join(_WORD.findall((text or "").lower()))


def content_tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if len(w) > 2 and w not in STOPWORDS}


def requirement_text(req: ReqView) -> str:
    return " ".join([req.title, req.description, *req.acceptance_criteria])


def evidence_supported(evidence: str, haystack: str) -> bool:
    """True when the quoted evidence really comes from the haystack (exact after normalising, or very close)."""
    quote = normalise(evidence)
    if len(quote) < 8:
        return False
    hay = normalise(haystack)
    if quote in hay:
        return True
    quote_tokens = content_tokens(evidence)
    if len(quote_tokens) < 3:
        return False
    return len(quote_tokens & content_tokens(haystack)) / len(quote_tokens) >= 0.85


def _case_text(case: Case) -> str:
    parts = [case.title, *case.preconditions]
    for step in case.steps:
        parts.append(step.get("action", ""))
        parts.append(step.get("expected", ""))
    return "\n".join(parts)


def unknown_terms(case: Case, allowed_text: str) -> list[str]:
    """Endpoints and code-style identifiers used by the case that the source material never mentions."""
    allowed = allowed_text.lower()
    allowed_norm = normalise(allowed_text)
    found: list[str] = []
    text = _case_text(case)
    for match in _PATH.findall(text):
        if match.lower() not in allowed and normalise(match) not in allowed_norm:
            found.append(match)
    for match in _IDENT.findall(text):
        if match.lower() not in allowed and normalise(match) not in allowed_norm:
            found.append(match)
    return list(dict.fromkeys(found))


def _near_duplicate(a: str, b: str) -> bool:
    ta, tb = content_tokens(a), content_tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.9


@dataclass
class GroundingReport:
    dropped: list[dict] = field(default_factory=list)
    warned: int = 0

    @property
    def dropped_count(self) -> int:
        return len(self.dropped)

    def reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.dropped:
            counts[item["reason"]] = counts.get(item["reason"], 0) + 1
        return counts


def check_cases(
    cases: list[Case],
    reqs: list[ReqView],
    knowledge_text: str = "",
    project_text: str = "",
    strict: bool = False,
) -> tuple[list[Case], GroundingReport]:
    by_code = {r.code: r for r in reqs}
    report = GroundingReport()
    kept: list[Case] = []
    for case in cases:
        linked = [by_code[c] for c in case.req_codes if c in by_code]
        source_text = " ".join(requirement_text(r) for r in linked)
        if not case.evidence.strip():
            report.dropped.append({"title": case.title, "reason": "no_evidence"})
            continue
        if not evidence_supported(case.evidence, f"{source_text} {knowledge_text}"):
            report.dropped.append({"title": case.title, "reason": "evidence_not_in_requirement"})
            continue
        extra = unknown_terms(case, f"{source_text} {knowledge_text} {project_text}")
        if extra:
            if strict:
                report.dropped.append({"title": case.title, "reason": "invented_terms", "terms": extra})
                continue
            case.warnings.append("Mentions terms not found in the requirement or project knowledge: " + ", ".join(extra))
            report.warned += 1
        if any(_near_duplicate(case.title, other.title) for other in kept):
            report.dropped.append({"title": case.title, "reason": "duplicate"})
            continue
        kept.append(case)
    return kept, report
