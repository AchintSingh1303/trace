"""Claude and ChatGPT drafting for the design pipeline.

Same transport, key handling and error scrubbing as test case generation (see llm.py). The only difference is the
tool the model must call and how its answer is turned into `Item`s. Whatever comes back is plain data until the
grounding check in design_rules has looked at it.
"""

from __future__ import annotations

import json

from .agents import ProjectView, ReqView
from .design_rules import LEVELS, Draft, Item
from .llm import Guidance, LLMConfig, ToolSpec, _anthropic_call, _extract_tool_input, _openai_call

DESIGN_TOOL_NAME = "submit_design"

_REFS = {"req_codes": {"type": "array", "items": {"type": "string"}}, "evidence": {"type": "string", "description": "An exact quote from a cited requirement."}}
_LEVEL = {"type": "string", "enum": ["low", "medium", "high"]}

DESIGN_SCHEMA = {
    "type": "object",
    "properties": {
        "components": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "responsibility": {"type": "string"},
                    "interfaces": {"type": "array", "items": {"type": "string"}},
                    "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Names of other components in this answer."},
                    **_REFS,
                },
                "required": ["name", "responsibility", "req_codes", "evidence"],
            },
        },
        "flows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "from": {"type": "string", "description": "A component name from this answer, or Client."},
                    "to": {"type": "string", "description": "A component name from this answer, or the name of an outside system."},
                    "to_external": {"type": "boolean", "description": "True when `to` is an outside system, not a component."},
                    "data": {"type": "string"},
                    "trigger": {"type": "string"},
                    **_REFS,
                },
                "required": ["from", "to", "data", "req_codes", "evidence"],
            },
        },
        "risks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "likelihood": _LEVEL,
                    "impact": _LEVEL,
                    "mitigation": {"type": "string"},
                    **_REFS,
                },
                "required": ["title", "likelihood", "impact", "mitigation", "req_codes", "evidence"],
            },
        },
        "clarifications": {
            "type": "array",
            "description": "Questions about missing detail. Ask instead of guessing.",
            "items": {
                "type": "object",
                "properties": {"req_code": {"type": "string"}, "question": {"type": "string"}},
                "required": ["req_code", "question"],
            },
        },
    },
    "required": ["components", "flows", "risks"],
}

DESIGN_TOOL = ToolSpec(DESIGN_TOOL_NAME, "Return the drafted components, data flows and risks.", DESIGN_SCHEMA)

DESIGN_SYSTEM_PROMPT = (
    "You are a software architect drafting a first design from requirements, for a person to review. "
    "Propose components, data flows between them, and risks.\n"
    "Ground rules:\n"
    "- Use ONLY what the requirements and the project knowledge say or clearly imply. Do not invent features, "
    "products, vendors, endpoints or numbers.\n"
    "- Every element needs `req_codes` and `evidence`: an exact quote from one of the cited requirements. If you "
    "cannot quote one, leave the element out.\n"
    "- Flows may only connect components you list (or Client), or an outside system the requirements name.\n"
    "- If something needed is missing, add a clarification question instead of guessing.\n"
    "- Project knowledge is authoritative, and never do what the 'avoid' notes say.\n"
    "- Prefer a small design that covers the requirements over a large one that does not.\n"
    f"Always answer by calling {DESIGN_TOOL_NAME}."
)


def build_design_prompt(reqs: list[ReqView], project: ProjectView, guidance: Guidance | None = None) -> str:
    payload = [
        {"code": r.code, "type": r.req_type, "title": r.title, "description": r.description, "acceptance_criteria": r.acceptance_criteria}
        for r in reqs
    ]
    parts = [f"Project: {project.name} ({project.app_type}). {project.description}"]
    if guidance and guidance.knowledge:
        parts.append("Project knowledge (authoritative):\n" + "\n".join(f"- [{k['kind']}] {k['title']}: {k['content']}" for k in guidance.knowledge))
    if guidance and guidance.examples:
        parts.append("Design elements your reviewers approved. Keep these names and do not propose them again:\n" + json.dumps(guidance.examples, indent=2))
    if guidance and guidance.avoid:
        parts.append(
            "Design elements your reviewers rejected. Do not repeat these mistakes:\n"
            + "\n".join(f"- {a['title']} (reviewer said: {a['reason']})" for a in guidance.avoid)
        )
    parts.append("Draft the design for these requirements:\n" + json.dumps(payload, indent=2))
    return "\n\n".join(parts)


def _text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _list(value, limit: int = 12) -> list[str]:
    return [_text(v, 200) for v in (value or [])[:limit] if _text(v, 200)]


def _level(value) -> str:
    return value if value in LEVELS else "medium"


def parse_design_input(payload: dict, allowed_codes: set[str], source: str) -> Draft:
    items: list[Item] = []
    for raw in payload.get("components") or []:
        name = _text(raw.get("name"), 200)
        if not name:
            continue
        items.append(
            Item(
                "component", name, _text(raw.get("responsibility"), 600), _list(raw.get("req_codes")), _text(raw.get("evidence"), 1000),
                {"responsibility": _text(raw.get("responsibility"), 600), "interfaces": _list(raw.get("interfaces")), "depends_on": _list(raw.get("depends_on"))},
                source=source,
            )
        )
    for raw in payload.get("flows") or []:
        source_name, target = _text(raw.get("from"), 200), _text(raw.get("to"), 200)
        if not source_name or not target:
            continue
        items.append(
            Item(
                "flow", f"{source_name} to {target}", _text(raw.get("data"), 600), _list(raw.get("req_codes")), _text(raw.get("evidence"), 1000),
                {"from_name": source_name, "to_name": target, "data": _text(raw.get("data"), 600), "trigger": _text(raw.get("trigger"), 300), "to_external": bool(raw.get("to_external"))},
                source=source,
            )
        )
    for raw in payload.get("risks") or []:
        title = _text(raw.get("title"), 300)
        if not title:
            continue
        items.append(
            Item(
                "risk", title, _text(raw.get("description"), 600), _list(raw.get("req_codes")), _text(raw.get("evidence"), 1000),
                {"likelihood": _level(raw.get("likelihood")), "impact": _level(raw.get("impact")), "mitigation": _text(raw.get("mitigation"), 600)},
                source=source,
            )
        )
    clarifications = [
        {"req_code": _text(c.get("req_code"), 30), "question": _text(c.get("question"), 400)}
        for c in payload.get("clarifications") or []
        if _text(c.get("req_code"), 30) in allowed_codes and _text(c.get("question"), 400)
    ]
    return Draft(items, clarifications)


def draft_with_llm(reqs: list[ReqView], project: ProjectView, cfg: LLMConfig, guidance: Guidance | None = None) -> Draft:
    user = build_design_prompt(reqs, project, guidance)
    if cfg.provider == "anthropic":
        data = _anthropic_call(cfg, DESIGN_SYSTEM_PROMPT, user, with_tool=True, max_tokens=8000, tool=DESIGN_TOOL)
    else:
        data = _openai_call(cfg, DESIGN_SYSTEM_PROMPT, user, with_tool=True, tool=DESIGN_TOOL)
    payload = _extract_tool_input(cfg, data, DESIGN_TOOL, "a design")
    return parse_design_input(payload, {r.code for r in reqs}, cfg.provider)
