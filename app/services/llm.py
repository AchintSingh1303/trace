"""Model-backed generators (Claude and ChatGPT) behind the same contract as the rule engine.

Requirements go in, validated `Case` objects come out. The two providers are called
over plain HTTPS with `httpx`, so there is nothing extra to install.

Handling of API keys:
  * a key arrives with the Generate request (or from an optional server env var),
  * it lives in memory for the duration of that run only,
  * it is never written to the database, the event stream, the audit log or logs,
  * any provider error text is scrubbed of the key before it can be shown.

Whatever the model returns is validated before it reaches the database: unknown
requirement codes are dropped, priorities are normalised, cases without steps
are discarded.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import httpx

from ..config import settings
from .agents import PRIORITY_RANK, Case, ProjectView, ReqView

PROVIDERS = {
    "anthropic": {
        "label": "Claude (Anthropic)",
        "default_model": "claude-sonnet-5",
        "url": "https://api.anthropic.com/v1/messages",
        "key_hint": "sk-ant-...",
    },
    "openai": {
        "label": "ChatGPT (OpenAI)",
        "default_model": "gpt-4o",
        "url": "https://api.openai.com/v1/chat/completions",
        "key_hint": "sk-...",
    },
}

TOOL_NAME = "submit_test_cases"
TOOL_DESCRIPTION = "Return the complete list of generated test cases."
TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "test_cases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Format: [Tag] Verify that ..."},
                    "subtype": {"type": "string"},
                    "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                    "req_codes": {"type": "array", "items": {"type": "string"}},
                    "evidence": {
                        "type": "string",
                        "description": "An exact quote from the requirement or acceptance criteria that this case verifies.",
                    },
                    "preconditions": {"type": "array", "items": {"type": "string"}},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"action": {"type": "string"}, "expected": {"type": "string"}},
                            "required": ["action", "expected"],
                        },
                    },
                    "test_data": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field_name": {"type": "string"},
                                "value": {"type": "string"},
                                "step_seq": {"type": "integer"},
                            },
                            "required": ["field_name", "value"],
                        },
                    },
                },
                "required": ["title", "priority", "req_codes", "evidence", "steps"],
            },
        },
        "clarifications": {
            "type": "array",
            "description": "Questions about ambiguous or missing detail. Ask instead of guessing.",
            "items": {
                "type": "object",
                "properties": {"req_code": {"type": "string"}, "question": {"type": "string"}},
                "required": ["req_code", "question"],
            },
        },
    },
    "required": ["test_cases"],
}

SYSTEM_PROMPT = (
    "You are a senior QA engineer. Write precise, independent, executable test cases. "
    "One action per step, each with an observable expected result.\n"
    "Ground rules:\n"
    "- Use ONLY facts stated in the requirements and the project knowledge. Do not invent "
    "endpoints, field names, limits, messages or behaviour.\n"
    "- Every case needs `evidence`: an exact quote from the requirement or its acceptance "
    "criteria that the case verifies. If you cannot quote one, do not write the case.\n"
    "- If a requirement is ambiguous or lacks a detail you need (a limit, a message, a rule), "
    "add a clarification question for it instead of guessing.\n"
    "- Project knowledge is authoritative. Follow its terms and rules, and never do what the "
    "'avoid' notes say.\n"
    f"Always answer by calling {TOOL_NAME}."
)


class LLMError(Exception):
    """A provider problem, with a message that is safe to show to the user."""


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    api_key: str = field(repr=False)
    model: str

    @property
    def label(self) -> str:
        return PROVIDERS[self.provider]["label"]


def normalise_provider(name: str | None) -> str:
    name = (name or "rules").strip().lower()
    return "anthropic" if name in {"llm", "claude"} else "openai" if name in {"chatgpt", "gpt"} else name


def resolve_config(provider: str | None, api_key: str | None, model: str | None) -> LLMConfig | None:
    """Combine per-request choices with optional server defaults. None means 'use the rule engine'."""
    chosen = normalise_provider(provider or settings.llm_provider)
    if chosen == "rules":
        return None
    if chosen not in PROVIDERS:
        raise ValueError(f"Unknown provider '{chosen}'. Use rules, anthropic or openai.")
    key = (api_key or settings.llm_api_key or "").strip()
    if not key:
        raise ValueError(f"Enter your {PROVIDERS[chosen]['label']} API key to use this generator.")
    return LLMConfig(chosen, key, (model or settings.llm_model or PROVIDERS[chosen]["default_model"]).strip())


# ---------------------------------------------------------------------------- transport


def _default_http() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0))


http_factory = _default_http  # tests replace this with a mock transport


def _scrub(text: str, cfg: LLMConfig) -> str:
    text = text.replace(cfg.api_key, "[your key]") if cfg.api_key else text
    return text if len(text) <= 300 else text[:297] + "..."


def _post(cfg: LLMConfig, headers: dict, payload: dict) -> dict:
    url = PROVIDERS[cfg.provider]["url"]
    try:
        with http_factory() as client:
            res = client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise LLMError(f"Could not reach {cfg.label}: {exc.__class__.__name__}") from None
    if res.status_code in (401, 403):
        raise LLMError(f"{cfg.label} rejected the API key ({res.status_code}). Check the key and that it can use this model.")
    if res.status_code >= 400:
        detail = ""
        try:
            body = res.json()
            err = body.get("error")
            detail = err.get("message", "") if isinstance(err, dict) else str(err or "")
        except ValueError:
            detail = res.text
        raise LLMError(_scrub(f"{cfg.label} returned {res.status_code}: {detail}".strip(), cfg))
    try:
        return res.json()
    except ValueError:
        raise LLMError(f"{cfg.label} sent a response that was not JSON") from None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict


CASES_TOOL = ToolSpec(TOOL_NAME, TOOL_DESCRIPTION, TOOL_SCHEMA)


def _anthropic_call(cfg: LLMConfig, system: str, user: str, with_tool: bool, max_tokens: int, tool: ToolSpec = CASES_TOOL) -> dict:
    payload: dict = {
        "model": cfg.model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if with_tool:
        payload["tools"] = [{"name": tool.name, "description": tool.description, "input_schema": tool.schema}]
        payload["tool_choice"] = {"type": "tool", "name": tool.name}
    headers = {"x-api-key": cfg.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    return _post(cfg, headers, payload)


def _openai_call(cfg: LLMConfig, system: str, user: str, with_tool: bool, tool: ToolSpec = CASES_TOOL) -> dict:
    payload: dict = {
        "model": cfg.model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    if with_tool:
        payload["tools"] = [
            {"type": "function", "function": {"name": tool.name, "description": tool.description, "parameters": tool.schema}}
        ]
        payload["tool_choice"] = {"type": "function", "function": {"name": tool.name}}
    headers = {"Authorization": f"Bearer {cfg.api_key}", "content-type": "application/json"}
    return _post(cfg, headers, payload)


def _extract_tool_input(cfg: LLMConfig, data: dict, tool: ToolSpec = CASES_TOOL, what: str = "test cases") -> dict:
    if cfg.provider == "anthropic":
        if data.get("stop_reason") == "max_tokens":
            raise LLMError("The answer was cut off. Use fewer requirements at a time.")
        for block in data.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == tool.name:
                return block.get("input") or {}
        raise LLMError(f"The model did not return {what} in the expected format")
    try:
        message = data["choices"][0]["message"]
        for call in message.get("tool_calls") or []:
            if call["function"]["name"] == tool.name:
                return json.loads(call["function"]["arguments"])
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    raise LLMError(f"The model did not return {what} in the expected format")


# ---------------------------------------------------------------------------- public API


@dataclass
class Guidance:
    """What the project has taught the generator: facts, approved examples, and past mistakes."""

    knowledge: list[dict] = field(default_factory=list)  # {kind, title, content}
    examples: list[dict] = field(default_factory=list)  # approved or corrected cases of this test type
    avoid: list[dict] = field(default_factory=list)  # rejected cases with the reviewer's reason

    def knowledge_text(self) -> str:
        return " ".join(f"{k['title']} {k['content']}" for k in self.knowledge)


@dataclass
class LLMResult:
    cases: list[Case]
    clarifications: list[dict] = field(default_factory=list)


def build_prompt(agent: str, targets: list[ReqView], project: ProjectView, guidance: Guidance | None = None) -> str:
    payload = [
        {
            "code": r.code,
            "type": r.req_type,
            "title": r.title,
            "description": r.description,
            "acceptance_criteria": r.acceptance_criteria,
        }
        for r in targets
    ]
    parts = [f"Project: {project.name} ({project.app_type}). {project.description}"]
    if guidance and guidance.knowledge:
        lines = [f"- [{k['kind']}] {k['title']}: {k['content']}" for k in guidance.knowledge]
        parts.append("Project knowledge (authoritative):\n" + "\n".join(lines))
    if guidance and guidance.examples:
        parts.append(
            "Cases your reviewers approved or corrected. Match their style, depth and wording:\n"
            + json.dumps(guidance.examples, indent=2)
        )
    if guidance and guidance.avoid:
        lines = [f"- {a['title']} (reviewer said: {a['reason']})" for a in guidance.avoid]
        parts.append("Cases your reviewers rejected. Do not repeat these mistakes:\n" + "\n".join(lines))
    parts.append(f"Generate {agent} test cases for these requirements:\n{json.dumps(payload, indent=2)}")
    return "\n\n".join(parts)


def parse_tool_input(payload: dict, test_type: str, allowed_codes: set[str], source: str = "llm") -> list[Case]:
    cases: list[Case] = []
    for raw in payload.get("test_cases", []):
        title = str(raw.get("title", "")).strip()
        steps = [
            {"action": str(s.get("action", "")).strip(), "expected": str(s.get("expected", "")).strip()}
            for s in raw.get("steps", [])
            if str(s.get("action", "")).strip()
        ]
        codes = [c for c in raw.get("req_codes", []) if c in allowed_codes]
        if not title or not steps or not codes:
            continue
        priority = raw.get("priority", "medium")
        if priority not in PRIORITY_RANK:
            priority = "medium"
        test_data = [
            {"field_name": str(d.get("field_name", "")), "value": str(d.get("value", "")), "step_seq": d.get("step_seq")}
            for d in raw.get("test_data", [])
            if d.get("field_name")
        ]
        cases.append(
            Case(
                title=title[:500],
                test_type=test_type,
                subtype=str(raw.get("subtype", "") or "requirement")[:50],
                priority=priority,
                steps=steps,
                req_codes=codes,
                preconditions=[str(p) for p in raw.get("preconditions", [])],
                test_data=test_data,
                evidence=str(raw.get("evidence", "") or "").strip()[:1000],
                source=source,
            )
        )
    return cases


def parse_clarifications(payload: dict, allowed_codes: set[str]) -> list[dict]:
    out = []
    for raw in payload.get("clarifications", []) or []:
        code, question = str(raw.get("req_code", "")), str(raw.get("question", "")).strip()
        if code in allowed_codes and question:
            out.append({"req_code": code, "question": question[:400]})
    return out


def generate_with_llm(
    agent: str, targets: list[ReqView], project: ProjectView, cfg: LLMConfig, guidance: Guidance | None = None
) -> LLMResult:
    user = build_prompt(agent, targets, project, guidance)
    if cfg.provider == "anthropic":
        data = _anthropic_call(cfg, SYSTEM_PROMPT, user, with_tool=True, max_tokens=8000)
    else:
        data = _openai_call(cfg, SYSTEM_PROMPT, user, with_tool=True)
    payload = _extract_tool_input(cfg, data)
    codes = {r.code for r in targets}
    return LLMResult(parse_tool_input(payload, agent, codes, source=cfg.provider), parse_clarifications(payload, codes))


def check_connection(cfg: LLMConfig) -> dict:
    """Cheapest possible round trip, used by the 'Test key' button."""
    started = time.time()
    if cfg.provider == "anthropic":
        _anthropic_call(cfg, "Reply with the single word OK.", "ping", with_tool=False, max_tokens=16)
    else:
        _openai_call(cfg, "Reply with the single word OK.", "ping", with_tool=False)
    return {"ok": True, "provider": cfg.provider, "model": cfg.model, "latency_ms": round((time.time() - started) * 1000)}
