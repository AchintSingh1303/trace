"""Specialised test-case agents.

Each agent turns the requirements it understands into structured test cases.
This module is the deterministic, offline implementation: no network, no model.
`llm.py` offers a drop-in replacement with the same `Case` output shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TEST_TYPES = ["functional", "api", "security", "performance", "integration"]
REQ_TYPES = ["Functional", "UI", "API", "Security", "Performance", "Journey"]

# Which requirement types each agent reads.
AGENT_INPUTS = {
    "functional": {"Functional", "UI"},
    "api": {"API"},
    "security": {"Security"},
    "performance": {"Performance"},
    "integration": {"Functional", "API", "Journey"},
}

CODE_PREFIX = {
    "functional": "FUNC",
    "api": "API",
    "security": "SEC",
    "performance": "PERF",
    "integration": "INTG",
}

SUBTYPE_LABELS = {
    "smoke": "Smoke",
    "happy_path": "Happy Path & Core Flows",
    "negative": "Negative & Error Handling",
    "boundary": "Boundary Values",
    "auth": "Authentication",
    "contract": "Contract & Schema",
    "error_handling": "Error Handling",
    "not_found": "Resource Not Found",
    "authentication": "Authentication",
    "authorization": "Authorization & Access Control",
    "input_validation": "Input Validation",
    "session_management": "Session Management",
    "data_exposure": "Sensitive Data Exposure",
    "requirement": "Requirement Specific",
    "baseline": "Baseline",
    "load": "Load",
    "stress": "Stress",
    "spike": "Spike",
    "soak": "Soak",
    "breakpoint": "Breakpoint",
    "journey": "End-to-End Journey",
    "data_consistency": "Data Consistency",
    "ui_api": "UI to API Consistency",
}

PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

NEGATIVE_WORDS = re.compile(
    r"\b(invalid|incorrect|wrong|error|errors|reject|rejected|rejects|not allowed|cannot|can't|"
    r"denied|locked|lock|fail|fails|failed|without|prevent|prevents|blocked|unauthori[sz]ed|"
    r"expired|empty|unavailable)\b",
    re.I,
)

FIELD_HINTS = [
    ("email", "user@example.com"),
    ("password", "P@ssw0rd!42"),
    ("username", "demo_user"),
    ("quantity", "2"),
    ("coupon", "SAVE10"),
    ("card", "4111 1111 1111 1111"),
    ("address", "221B Baker Street"),
    ("phone", "+91 98765 43210"),
    ("search", "wireless headphones"),
    ("otp", "123456"),
    ("name", "Asha Verma"),
    ("page", "1"),
    ("limit", "20"),
]

STOPWORDS = {
    "user", "users", "system", "should", "shall", "must", "that", "with", "from", "when",
    "into", "able", "allow", "allows", "application", "feature", "page", "screen", "their",
    "have", "will", "been", "each", "every", "only", "also", "than", "then", "this", "these",
    "valid", "invalid", "data", "item", "items",
}

BOUNDARY_RE = re.compile(
    r"(\d+)\s+(?:failed\s+)?(characters?|chars?|items?|attempts?|times?|units?|results?|records?|"
    r"orders?|minutes?|per\s+\w+)",
    re.I,
)


@dataclass
class ReqView:
    id: int
    code: str
    title: str
    description: str
    req_type: str
    acceptance_criteria: list[str]
    version: int


@dataclass
class ProjectView:
    name: str
    app_type: str = "web"
    description: str = ""


@dataclass
class Case:
    title: str
    test_type: str
    subtype: str
    priority: str
    steps: list[dict]
    req_codes: list[str]
    preconditions: list[str] = field(default_factory=list)
    test_data: list[dict] = field(default_factory=list)
    evidence: str = ""
    warnings: list[str] = field(default_factory=list)
    source: str = "rules"

    @property
    def tag(self) -> str:
        match = re.match(r"\s*\[([^\]]+)\]", self.title)
        return match.group(1) if match else ""


# --------------------------------------------------------------------------- helpers


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).rstrip(".")


def lower_first(text: str) -> str:
    text = clean(text)
    if len(text) > 1 and text[0].isupper() and not text[1].isupper():
        return text[0].lower() + text[1:]
    return text


def short(text: str, limit: int = 160) -> str:
    text = clean(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def fields_for(*texts: str, limit: int = 4) -> list[tuple[str, str]]:
    blob = " ".join(texts).lower()
    found = [(name, value) for name, value in FIELD_HINTS if name in blob]
    return found[:limit]


def tokens(req: ReqView) -> set[str]:
    words = re.findall(r"[a-z]{4,}", f"{req.title} {req.description}".lower())
    return {w for w in words if w not in STOPWORDS}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "resource"


def _step(action: str, expected: str) -> dict:
    return {"action": action, "expected": expected}


# --------------------------------------------------------------------------- boundary


def boundary_case(req: ReqView, ac: str, test_type: str, flavor: str = "ui") -> Case | None:
    match = BOUNDARY_RE.search(ac)
    if not match:
        return None
    n = int(match.group(1))
    unit = re.sub(r"\s+", " ", match.group(2).lower())
    low = ac.lower()
    if re.search(r"at least|minimum|min\b|no fewer than|no less than", low):
        direction = "lower"
    elif re.search(r"at most|maximum|max\b|up to|limited to|no more than|not more than", low):
        direction = "upper"
    elif re.search(r"after|within|exceed", low):
        direction = "threshold"
    else:
        direction = "unknown"

    expected = {
        "lower": ("The value is rejected with a clear validation message", "The value is accepted", "The value is accepted"),
        "upper": ("The value is accepted", "The value is accepted", "The value is rejected with a clear validation message"),
        "threshold": ("The rule is not triggered yet", "The rule is triggered as specified", "The rule continues to apply"),
        "unknown": (f"Behaviour is consistent with: {short(ac, 90)}",) * 3,
    }[direction]

    below, at, above = max(n - 1, 0), n, n + 1
    if flavor == "api":
        where = "Send the request with the parameter set to"
        pre = "Authenticate with a valid token"
    else:
        where = "Enter a value of"
        pre = f"The {req.title} screen is displayed"
    steps = [
        _step(pre, "The form or endpoint is ready to accept input"),
        _step(f"{where} {below} {unit} (just below the limit)", expected[0]),
        _step(f"{where} {at} {unit} (exactly at the limit)", expected[1]),
        _step(f"{where} {above} {unit} (just above the limit)", expected[2]),
    ]
    data = [
        {"field_name": "boundary_below", "value": str(below), "step_seq": 2},
        {"field_name": "boundary_at", "value": str(at), "step_seq": 3},
        {"field_name": "boundary_above", "value": str(above), "step_seq": 4},
    ]
    return Case(
        title=f"[Boundary] Verify the limit of {n} {unit}: {short(ac, 100)}",
        test_type=test_type,
        subtype="boundary",
        priority="medium",
        steps=steps,
        req_codes=[req.code],
        preconditions=[f"Requirement {req.code} is implemented"],
        test_data=data,
    )


# --------------------------------------------------------------------------- functional


def functional_agent(targets: list[ReqView], all_reqs: list[ReqView]) -> list[Case]:
    cases: list[Case] = []
    for req in targets:
        pre = [f"The {req.title} feature is enabled", "User is signed in where authentication is required"]
        nav = _step(f"Navigate to the '{req.title}' screen", f"The '{req.title}' screen is displayed")
        cases.append(
            Case(
                title=f"[Smoke] Verify that the {req.title} feature is available",
                test_type="functional",
                subtype="smoke",
                priority="high",
                steps=[nav, _step("Check the primary controls on the screen", "All primary controls are visible and enabled")],
                req_codes=[req.code],
                preconditions=pre,
            )
        )
        criteria = req.acceptance_criteria or [req.title]
        has_negative = False
        for ac in criteria:
            fields = fields_for(req.title, req.description, ac)
            if NEGATIVE_WORDS.search(ac):
                has_negative = True
                data = [
                    {"field_name": name, "value": f"invalid_{name}", "step_seq": 2} for name, _ in fields
                ]
                cases.append(
                    Case(
                        title=f"[Negative] Verify that {lower_first(short(ac, 130))}",
                        test_type="functional",
                        subtype="negative",
                        priority="medium",
                        steps=[
                            nav,
                            _step(f"Trigger the condition: {short(ac)}", "The system rejects the attempt and shows a clear message"),
                            _step("Check the application state afterwards", f"No unintended change occurs; behaviour matches: {short(ac, 100)}"),
                        ],
                        req_codes=[req.code],
                        preconditions=pre,
                        test_data=data,
                    )
                )
            else:
                steps = [nav]
                data = []
                if fields:
                    steps.append(
                        _step(
                            "Enter valid values for: " + ", ".join(name for name, _ in fields),
                            "The values are accepted without validation errors",
                        )
                    )
                    data = [{"field_name": n, "value": v, "step_seq": 2} for n, v in fields]
                steps.append(_step(f"Perform the action: {short(ac)}", clean(ac)))
                cases.append(
                    Case(
                        title=f"[Happy Path] Verify that {lower_first(short(ac, 130))}",
                        test_type="functional",
                        subtype="happy_path",
                        priority="high",
                        steps=steps,
                        req_codes=[req.code],
                        preconditions=pre,
                        test_data=data,
                    )
                )
            boundary = boundary_case(req, ac, "functional")
            if boundary:
                cases.append(boundary)
        if not has_negative:
            cases.append(
                Case(
                    title=f"[Negative] Verify that {req.title} rejects empty required fields",
                    test_type="functional",
                    subtype="negative",
                    priority="medium",
                    steps=[
                        nav,
                        _step("Submit the form with all required fields empty", "Validation messages appear next to each required field"),
                        _step("Check that nothing was saved", "No record is created and the user stays on the screen"),
                    ],
                    req_codes=[req.code],
                    preconditions=pre,
                )
            )
    return cases


# --------------------------------------------------------------------------- api

ENDPOINT_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[A-Za-z0-9_\-/{}.:]*)")


def _endpoint(req: ReqView) -> tuple[str, str]:
    match = ENDPOINT_RE.search(f"{req.title} {req.description} {' '.join(req.acceptance_criteria)}")
    if match:
        return match.group(1), match.group(2)
    return "GET", f"/api/{slug(req.title)}"


def api_agent(targets: list[ReqView], all_reqs: list[ReqView]) -> list[Case]:
    cases: list[Case] = []
    for req in targets:
        method, path = _endpoint(req)
        call = f"{method} {path}"
        base_data = [
            {"field_name": "method", "value": method, "step_seq": 1},
            {"field_name": "endpoint", "value": path, "step_seq": 1},
        ]
        cases.append(
            Case(
                title=f"[Auth] Verify that {call} rejects requests without a valid token",
                test_type="api",
                subtype="auth",
                priority="high",
                steps=[
                    _step(f"Send {call} with no Authorization header", "Response status is 401 and no data is returned"),
                    _step(f"Send {call} with an expired token", "Response status is 401 with an error body"),
                ],
                req_codes=[req.code],
                test_data=base_data,
            )
        )
        for ac in req.acceptance_criteria or [req.title]:
            cases.append(
                Case(
                    title=f"[Contract] Verify that {lower_first(short(ac, 130))}",
                    test_type="api",
                    subtype="contract",
                    priority="high",
                    steps=[
                        _step("Authenticate with a valid token", "A bearer token is available"),
                        _step(f"Send {call} with a valid payload", "Response status is 2xx and the body matches the documented schema"),
                        _step(f"Check the behaviour: {short(ac)}", clean(ac)),
                    ],
                    req_codes=[req.code],
                    preconditions=["A valid test account exists"],
                    test_data=base_data,
                )
            )
            boundary = boundary_case(req, ac, "api", flavor="api")
            if boundary:
                cases.append(boundary)
        cases.append(
            Case(
                title=f"[Error Handling] Verify that {call} rejects an invalid payload",
                test_type="api",
                subtype="error_handling",
                priority="medium",
                steps=[
                    _step(f"Send {call} with a malformed JSON body", "Response status is 400"),
                    _step("Inspect the error body", "It contains a machine-readable code and a human-readable message"),
                ],
                req_codes=[req.code],
                test_data=base_data,
            )
        )
        if "{" in path:
            cases.append(
                Case(
                    title=f"[Not Found] Verify that {call} returns 404 for an unknown id",
                    test_type="api",
                    subtype="not_found",
                    priority="medium",
                    steps=[_step(f"Send {call} with an id that does not exist", "Response status is 404 and the body explains the resource was not found")],
                    req_codes=[req.code],
                    test_data=base_data,
                )
            )
    return cases


# --------------------------------------------------------------------------- security

SEC_KEYWORDS = {
    "authentication": r"login|password|credential|sign[- ]?in|authenticat|lock",
    "authorization": r"role|permission|access|authori[sz]|admin|other user|own ",
    "input_validation": r"input|inject|sanitis|sanitiz|validat|script|payload|upload",
    "session_management": r"session|token|cookie|logout|expire|timeout",
    "data_exposure": r"encrypt|pii|sensitive|expos|leak|mask|personal|card|log",
}

SEC_LIBRARY = {
    "authentication": [
        (
            "Verify that {t} cannot be used without valid credentials",
            [("Request the protected action without signing in", "The request is redirected or rejected with 401"),
             ("Retry with an incorrect password", "Access is denied and the message does not reveal which field was wrong")],
            "critical",
            [("password", "wrong-password")],
        ),
        (
            "Verify that repeated failed attempts on {t} are throttled",
            [("Submit 10 failed attempts in quick succession", "Requests are throttled or the account is temporarily locked"),
             ("Attempt a valid sign-in during the lock window", "The attempt is refused until the lock expires")],
            "high",
            [("attempts", "10")],
        ),
    ],
    "authorization": [
        (
            "Verify that {t} blocks access to another user's data (IDOR)",
            [("Sign in as user A and note the id of a record they own", "Record id captured"),
             ("Sign in as user B and request user A's record by id", "Response is 403 or 404 and no data is returned")],
            "critical",
            [("target_record_id", "A-1001")],
        ),
        (
            "Verify that role restrictions are enforced for {t}",
            [("Sign in with a low-privilege role", "Session established"),
             ("Call an admin-only action for this feature", "Response is 403 and the action is not performed")],
            "high",
            [("role", "viewer")],
        ),
    ],
    "input_validation": [
        (
            "Verify that {t} rejects SQL injection payloads",
            [("Submit the payload in each text input and query parameter", "The payload is rejected or safely escaped"),
             ("Inspect the response and server behaviour", "No database error is exposed and no extra rows are returned")],
            "critical",
            [("payload", "' OR '1'='1' --")],
        ),
        (
            "Verify that {t} neutralises script injection (XSS)",
            [("Submit a script payload in each free-text field", "The input is accepted or rejected without executing"),
             ("Render the stored value on every page that shows it", "The payload is displayed as inert text")],
            "high",
            [("payload", "<script>alert(1)</script>")],
        ),
    ],
    "session_management": [
        (
            "Verify that sessions for {t} expire after inactivity",
            [("Sign in and stay idle past the configured timeout", "The session expires"),
             ("Use the old session token on a protected call", "Response is 401 and the user is asked to sign in again")],
            "high",
            [("idle_minutes", "30")],
        ),
        (
            "Verify that tokens are invalidated on logout for {t}",
            [("Sign out", "The user is signed out"),
             ("Replay the previous token", "Response is 401")],
            "high",
            [],
        ),
    ],
    "data_exposure": [
        (
            "Verify that sensitive data is not exposed by {t}",
            [("Exercise the feature and capture responses and logs", "Traffic is encrypted in transit"),
             ("Search responses and logs for secrets or personal data", "Sensitive values are masked or absent")],
            "high",
            [],
        ),
    ],
}


def security_agent(targets: list[ReqView], all_reqs: list[ReqView]) -> list[Case]:
    cases: list[Case] = []
    for req in targets:
        blob = f"{req.title} {req.description} {' '.join(req.acceptance_criteria)}".lower()
        categories = [c for c, pattern in SEC_KEYWORDS.items() if re.search(pattern, blob)]
        categories = categories or ["authentication", "input_validation"]
        for category in categories:
            for title, steps, priority, data in SEC_LIBRARY[category]:
                cases.append(
                    Case(
                        title=f"[Security] {title.format(t=req.title)}",
                        test_type="security",
                        subtype=category,
                        priority=priority,
                        steps=[_step(a, e) for a, e in steps],
                        req_codes=[req.code],
                        preconditions=["A test account exists in a non-production environment"],
                        test_data=[{"field_name": n, "value": v, "step_seq": 1} for n, v in data],
                    )
                )
        for ac in req.acceptance_criteria:
            cases.append(
                Case(
                    title=f"[Security] Verify that {lower_first(short(ac, 130))}",
                    test_type="security",
                    subtype="requirement",
                    priority="high",
                    steps=[
                        _step(f"Attempt to violate the rule: {short(ac)}", "The attempt is blocked or has no effect"),
                        _step("Review audit logs for the attempt", "The attempt is recorded without sensitive data"),
                    ],
                    req_codes=[req.code],
                    preconditions=["A test account exists in a non-production environment"],
                )
            )
    return cases


# --------------------------------------------------------------------------- performance

PERF_PRIORITY = {"breakpoint": "critical", "stress": "critical", "spike": "high", "load": "high", "soak": "medium", "baseline": "low"}


def _perf_numbers(req: ReqView) -> tuple[int, int]:
    blob = f"{req.title} {req.description} {' '.join(req.acceptance_criteria)}".lower()
    users = re.search(r"(\d+)\s*(?:concurrent\s+)?(?:virtual\s+)?users", blob)
    p95 = re.search(r"p9\d\s*(?:<|<=|under|below|of)?\s*(\d+)\s*(ms|s)\b", blob)
    n_users = int(users.group(1)) if users else 100
    p95_ms = 1000
    if p95:
        p95_ms = int(p95.group(1)) * (1000 if p95.group(2) == "s" else 1)
    return n_users, p95_ms


def performance_agent(targets: list[ReqView], all_reqs: list[ReqView]) -> list[Case]:
    cases: list[Case] = []
    for req in targets:
        users, p95 = _perf_numbers(req)
        blob = f"{req.title} {req.description} {' '.join(req.acceptance_criteria)}".lower()
        scenarios = {
            "baseline": (1, 300, "p95 stays at or below the target with a single user"),
            "load": (users, 900, f"p95 <= {p95} ms and error rate < 1% at {users} users"),
            "stress": (users * 2, 600, "The system degrades gracefully; no crash and errors stay below 5%"),
            "spike": (users * 5, 120, "Latency recovers to target within 2 minutes after the spike"),
            "soak": (users, 7200, "No memory growth trend and p95 stays within 10% of the load result"),
        }
        if re.search(r"limit|capacity|maximum|breakpoint|peak", blob):
            scenarios["breakpoint"] = (users * 10, 1800, "The breaking point is identified and recorded")
        for scenario, (peak, duration, expected) in scenarios.items():
            cases.append(
                Case(
                    title=f"[{scenario.title()}] Verify {req.title} under a {scenario} workload ({peak} users)",
                    test_type="performance",
                    subtype=scenario,
                    priority=PERF_PRIORITY[scenario],
                    steps=[
                        _step(f"Configure the {scenario} scenario with {peak} virtual users", "The workload is configured"),
                        _step(f"Run the scenario for {duration} seconds at steady state", "The run completes without harness errors"),
                        _step("Collect latency percentiles, throughput and error rate", expected),
                    ],
                    req_codes=[req.code],
                    preconditions=["A production-like environment is available", f"Target p95: {p95} ms"],
                    test_data=[
                        {"field_name": "tool", "value": "k6", "step_seq": 1},
                        {"field_name": "workload_mode", "value": scenario, "step_seq": 1},
                        {"field_name": "peak_value", "value": str(peak), "step_seq": 1},
                        {"field_name": "think_time_ms", "value": "1000", "step_seq": 1},
                        {"field_name": "steady_state_sec", "value": str(duration), "step_seq": 2},
                        {"field_name": "target_p95_ms", "value": str(p95), "step_seq": 3},
                    ],
                )
            )
    return cases


# --------------------------------------------------------------------------- integration


def integration_agent(targets: list[ReqView], all_reqs: list[ReqView]) -> list[Case]:
    cases: list[Case] = []
    pool = [r for r in all_reqs if r.req_type in ("Functional", "UI", "API", "Journey")]

    def related(req: ReqView, wanted: set[str], limit: int) -> list[ReqView]:
        mine = tokens(req)
        scored = [(len(mine & tokens(o)), o) for o in pool if o.id != req.id and o.req_type in wanted]
        scored = [s for s in scored if s[0] > 0]
        scored.sort(key=lambda s: (-s[0], s[1].code))
        return [o for _, o in scored[:limit]]

    for req in targets:
        if req.req_type == "Journey":
            links = related(req, {"Functional", "UI", "API"}, 3)
            codes = [req.code] + [o.code for o in links]
            steps = [_step("Start from a clean state as a new visitor", "The landing page loads")]
            for ac in req.acceptance_criteria or [req.title]:
                steps.append(_step(f"Continue the journey: {short(ac)}", clean(ac)))
            cases.append(
                Case(
                    title=f"[Journey] Verify the end-to-end flow: {req.title}",
                    test_type="integration",
                    subtype="journey",
                    priority="high",
                    steps=steps,
                    req_codes=codes,
                    preconditions=["All dependent services are running", "Test data is seeded"],
                )
            )
            cases.append(
                Case(
                    title=f"[Data Consistency] Verify that data stays consistent across steps of: {req.title}",
                    test_type="integration",
                    subtype="data_consistency",
                    priority="medium",
                    steps=[
                        _step("Complete the journey once and note the ids and totals shown to the user", "Values captured"),
                        _step("Compare them with what the API and the database report", "All values match across UI, API and storage"),
                    ],
                    req_codes=codes,
                )
            )
        elif req.req_type == "API":
            for other in related(req, {"Functional", "UI"}, 1):
                method, path = _endpoint(req)
                cases.append(
                    Case(
                        title=f"[UI-API] Verify that '{other.title}' and {method} {path} agree",
                        test_type="integration",
                        subtype="ui_api",
                        priority="medium",
                        steps=[
                            _step(f"Perform '{other.title}' in the UI", "The UI shows a success state"),
                            _step(f"Call {method} {path} directly with the same inputs", "The API response matches what the UI displayed"),
                        ],
                        req_codes=[req.code, other.code],
                    )
                )
    return cases


AGENTS = {
    "functional": functional_agent,
    "api": api_agent,
    "security": security_agent,
    "performance": performance_agent,
    "integration": integration_agent,
}


# --------------------------------------------------------------------------- public helpers


def run_agent(agent: str, targets: list[ReqView], all_reqs: list[ReqView]) -> list[Case]:
    cases = AGENTS[agent](targets, all_reqs)
    seen: set[str] = set()
    unique: list[Case] = []
    for case in cases:
        key = case.title.lower()
        if key not in seen:
            seen.add(key)
            unique.append(case)
    return unique


def sort_and_cap(cases: list[Case], cap: int | None) -> list[Case]:
    ordered = sorted(cases, key=lambda c: PRIORITY_RANK.get(c.priority, 9))
    return ordered[:cap] if cap is not None else cases


def split_cap(max_tcs: int | None, agents: list[str]) -> dict[str, int | None]:
    if max_tcs is None:
        return {a: None for a in agents}
    base, extra = divmod(max_tcs, len(agents))
    return {a: base + (1 if i < extra else 0) for i, a in enumerate(agents)}


def partition(agent: str, reqs: list[ReqView]) -> list[ReqView]:
    wanted = AGENT_INPUTS[agent]
    return [r for r in reqs if r.req_type in wanted]


def subtype_summary(cases: list[Case]) -> list[dict]:
    counts: dict[str, int] = {}
    for case in cases:
        counts[case.subtype] = counts.get(case.subtype, 0) + 1
    return [
        {"sub_type": key, "label": SUBTYPE_LABELS.get(key, key.replace("_", " ").title()), "count": n}
        for key, n in counts.items()
    ]

