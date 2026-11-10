"""Design drafting: shared types, the built-in rules and the grounding check.

The rules read requirement text and draft three kinds of element (components, data flows and risks). They are
keyword based, which is their limit: they find the usual parts of a business application and miss anything unusual.
That is why every element carries the sentence it came from and goes through review before it counts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .agents import ENDPOINT_RE, ReqView
from .grounding import content_tokens, evidence_supported, requirement_text
from .quality import Subject, check_requirement

KINDS = ("component", "flow", "risk")
CODE_PREFIX = {"component": "CMP", "flow": "FLW", "risk": "RSK"}
LEVELS = {"low": 1, "medium": 2, "high": 3}
# likelihood x impact: 6 or 9 is high, 3 or 4 is medium, 1 or 2 is low
SEVERITY_HIGH = 6
SEVERITY_MEDIUM = 3
DUPLICATE_OVERLAP = 0.9

CLIENT = "Client"  # an outside caller, not a component


def severity(likelihood: str, impact: str) -> str:
    score = LEVELS.get(likelihood, 2) * LEVELS.get(impact, 2)
    if score >= SEVERITY_HIGH:
        return "high"
    return "medium" if score >= SEVERITY_MEDIUM else "low"


@dataclass
class Item:
    """One drafted element. Relationships use names; saving turns them into codes."""

    kind: str
    title: str
    summary: str
    req_codes: list[str]
    evidence: str
    detail: dict = field(default_factory=dict)
    source: str = "rules"
    warnings: list[str] = field(default_factory=list)


@dataclass
class Draft:
    items: list[Item] = field(default_factory=list)
    clarifications: list[dict] = field(default_factory=list)


# --------------------------------------------------------------------------- reading requirements


def units(req: ReqView) -> list[str]:
    """Every sentence-sized piece of a requirement, so a rule can quote exactly the one it used."""
    out = [req.title.strip()]
    for part in re.split(r"(?<=[.!?])\s+", req.description or ""):
        if part.strip():
            out.append(part.strip())
    out.extend(c.strip() for c in req.acceptance_criteria if c.strip())
    return out


def first_match(req: ReqView, pattern: re.Pattern) -> str | None:
    for unit in units(req):
        if pattern.search(unit):
            return unit
    return None


def _ci(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


# --------------------------------------------------------------------------- rule tables

# name, what it is responsible for, keywords
AREAS = [
    ("Authentication and sessions", "Signs users in, keeps their session and enforces lockout", _ci(r"\b(log ?in|sign ?in|password|session|credential|lock(ed|out)|authenticat\w*)\b")),
    ("Access control", "Decides what each user is allowed to see and do", _ci(r"\b(roles?|permissions?|authori[sz]\w*|access control|their own)\b")),
    ("Shopping cart", "Holds the items a shopper has chosen", _ci(r"\b(cart|basket)\b")),
    ("Checkout and payments", "Takes an order through payment and confirmation", _ci(r"\b(checkout|payments?|card|coupon|refund|invoice)\b")),
    ("Order management", "Creates and tracks orders", _ci(r"\borders?\b")),
    ("Product catalogue", "Serves product details, prices and stock", _ci(r"\b(products?|catalogue|catalog|inventory|stock)\b")),
    ("Search", "Finds items by keyword", _ci(r"\bsearch\w*\b")),
    ("Notifications", "Sends messages to users", _ci(r"\b(e-?mail (confirmation|notification|receipt|alert|is sent)|sends? an? e-?mail|sms|notifications?|alerts?|reminders?)\b")),
    ("Reporting", "Builds reports and exports", _ci(r"\b(reports?|exports?|analytics)\b")),
    ("User profile", "Stores account details and preferences", _ci(r"\b(profile|account settings|preferences)\b")),
]
MAX_AREAS_PER_REQUIREMENT = 2

STORE_RE = _ci(r"\b(stor(e|es|ed|age)|persist\w*|saved?|saves|history)\b")

# name, what it is, the component that most likely owns the call, keywords. These become outside systems in a data flow.
EXTERNALS = [
    ("Payment provider", "Takes card payments", "Checkout and payments", _ci(r"\b(payment (gateway|provider)|card payments?|pay(s|ing)? .{0,40}\bcard)\b")),
    ("Email service", "Delivers email", "Notifications", _ci(r"\b(e-?mail (confirmation|notification|receipt|is sent)|sends? an? e-?mail)\b")),
    ("SMS service", "Delivers text messages", "Notifications", _ci(r"\b(sms|text messages?)\b")),
    ("Identity provider", "Signs users in through another service", "Authentication and sessions", _ci(r"\b(sso|single sign-?on|oauth|saml)\b")),
]

# keywords, title, likelihood, impact, mitigation
SECURITY_RISKS = [
    (_ci(r"\b(session|lock(ed|out)|password|credential)s?\b"), "Weak sign-in or session handling", "medium", "high",
     "Hash passwords, expire sessions, lock accounts after repeated failures and test each rule."),
    (_ci(r"\b(card numbers?|sensitive|personal data|pii|never written to logs)\b"), "Sensitive data exposed", "medium", "high",
     "Keep sensitive values out of logs and responses, and mask them wherever they are shown."),
    (_ci(r"\b(sanitis\w*|sanitiz\w*|validated|injection|malicious input|free-text)\b"), "Malicious input reaches storage", "medium", "high",
     "Validate on the server, use parameterised queries and encode output."),
    (_ci(r"\b(their own|permissions?|access control|authori[sz]\w*)\b"), "One user can reach another user's data", "medium", "high",
     "Check ownership on every request, not only in the interface."),
]


# --------------------------------------------------------------------------- the rule drafter


def _slug_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip().strip(".")) or "Untitled"


def draft_rules(reqs: list[ReqView]) -> Draft:
    items: list[Item] = []
    by_code = {r.code: r for r in reqs}

    # ---- components from named areas
    areas: dict[str, dict] = {}

    def area(name: str, summary: str) -> dict:
        return areas.setdefault(name, {"summary": summary, "codes": [], "evidence": None, "stores": False})

    for req in reqs:
        text = requirement_text(req)
        matched = 0
        for name, summary, pattern in AREAS:
            if matched >= MAX_AREAS_PER_REQUIREMENT:
                break
            unit = first_match(req, pattern)
            if not unit:
                continue
            matched += 1
            entry = area(name, summary)
            entry["codes"].append(req.code)
            entry["evidence"] = entry["evidence"] or unit
            entry["stores"] = entry["stores"] or bool(STORE_RE.search(text))
        if not matched and req.req_type in ("Functional", "Journey"):
            entry = area(_slug_title(req.title), f"Implements: {_slug_title(req.title)}")
            entry["codes"].append(req.code)
            entry["evidence"] = entry["evidence"] or req.title
            entry["stores"] = bool(STORE_RE.search(text))

    # ---- structural components the requirements call for
    ui_reqs = [r for r in reqs if r.req_type == "UI"]
    endpoints: list[tuple[str, str, ReqView, str]] = []
    for req in reqs:
        if req.req_type != "API":
            continue
        for unit in units(req):
            for method, path in ENDPOINT_RE.findall(unit):
                endpoints.append((method, path, req, unit))
    api_reqs = [r for r in reqs if r.req_type == "API"]
    store_hits = [(r, u) for r in reqs for u in units(r) if STORE_RE.search(u)]

    components: list[Item] = []
    if ui_reqs:
        components.append(
            Item("component", "Web UI", "The screens users work in.", [r.code for r in ui_reqs], ui_reqs[0].title,
                 {"responsibility": "Shows the screens described by the interface requirements.", "interfaces": [], "depends_on": ["API layer"] if api_reqs else []})
        )
    if api_reqs:
        seen_paths: list[str] = []
        for method, path, _, _ in endpoints:
            label = f"{method} {path}"
            if label not in seen_paths:
                seen_paths.append(label)
        first_unit = endpoints[0][3] if endpoints else api_reqs[0].title
        components.append(
            Item("component", "API layer", "The interface other parts call.", [r.code for r in api_reqs], first_unit,
                 {"responsibility": "Serves the endpoints named in the API requirements.", "interfaces": seen_paths, "depends_on": ["Data store"] if store_hits else []})
        )
    if store_hits:
        codes = list(dict.fromkeys(r.code for r, _ in store_hits))
        components.append(
            Item("component", "Data store", "Where the system keeps what it must remember.", codes, store_hits[0][1],
                 {"responsibility": "Keeps the records the requirements say are saved or kept as history.", "interfaces": [], "depends_on": []})
        )
    have_store = any(c.title == "Data store" for c in components)
    for name, entry in areas.items():
        components.append(
            Item(
                "component", name, entry["summary"], list(dict.fromkeys(entry["codes"])), entry["evidence"],
                {"responsibility": entry["summary"] + ".", "interfaces": [], "depends_on": ["Data store"] if (have_store and entry["stores"]) else []},
            )
        )
    items.extend(components)
    component_names = {c.title for c in components}

    # ---- flows from endpoints
    caller = "Web UI" if "Web UI" in component_names else CLIENT
    seen_flows: set[str] = set()
    for method, path, req, unit in endpoints:
        title = f"{method} {path}"
        if title in seen_flows:
            continue
        seen_flows.add(title)
        items.append(
            Item("flow", title, f"{caller} calls {title}.", [req.code], unit,
                 {"from_name": caller, "to_name": "API layer", "data": req.title, "trigger": "The caller sends the request"})
        )
    if "API layer" in component_names and "Data store" in component_names:
        hit = store_hits[0]
        items.append(
            Item("flow", "API layer to Data store", "The API layer reads and writes saved records.", list(dict.fromkeys(r.code for r, _ in store_hits)), hit[1],
                 {"from_name": "API layer", "to_name": "Data store", "data": "Saved records", "trigger": "A request reads or changes saved data"})
        )

    # ---- outside systems, as flows and as risks
    for name, summary, preferred, pattern in EXTERNALS:
        hits = [(r, first_match(r, pattern)) for r in reqs]
        hits = [(r, u) for r, u in hits if u]
        if not hits:
            continue
        codes = list(dict.fromkeys(r.code for r, _ in hits))
        shared = [c.title for c in components if set(c.req_codes) & set(codes) and c.title not in ("Web UI", "API layer", "Data store")]
        owner = preferred if preferred in shared else (shared[0] if shared else None)
        source = owner or ("API layer" if "API layer" in component_names else CLIENT)
        items.append(
            Item("flow", f"{source} to {name}", f"{source} depends on the {name.lower()}.", codes, hits[0][1],
                 {"from_name": source, "to_name": name, "data": summary, "trigger": "The requirement needs the outside service", "to_external": True})
        )
        items.append(
            Item("risk", f"{name} is slow or unavailable", f"{source} cannot finish its work while the {name.lower()} is down.", codes, hits[0][1],
                 {"likelihood": "medium", "impact": "medium", "mitigation": "Set timeouts, retry safely and show a clear message when the service cannot be reached."})
        )

    # ---- risks from security wording
    merged: dict[str, dict] = {}
    for req in reqs:
        if req.req_type != "Security":
            continue
        for pattern, title, likelihood, impact, mitigation in SECURITY_RISKS:
            unit = first_match(req, pattern)
            if not unit:
                continue
            entry = merged.setdefault(title, {"codes": [], "evidence": unit, "likelihood": likelihood, "impact": impact, "mitigation": mitigation})
            entry["codes"].append(req.code)
    for title, entry in merged.items():
        items.append(
            Item("risk", title, "A security requirement depends on this being done well.", list(dict.fromkeys(entry["codes"])), entry["evidence"],
                 {"likelihood": entry["likelihood"], "impact": entry["impact"], "mitigation": entry["mitigation"]})
        )

    # ---- risks from performance targets
    for req in reqs:
        if req.req_type != "Performance":
            continue
        numbered = next((u for u in units(req) if re.search(r"\d", u)), None)
        if numbered:
            items.append(
                Item("risk", f"Performance target may not hold: {_slug_title(req.title)}", "The design has to meet a measured target.", [req.code], numbered,
                     {"likelihood": "medium", "impact": "high", "mitigation": "Load test early against the stated target, then add caching or indexing where it misses."})
            )

    # ---- risks from requirements that are not ready to build from
    for req in reqs:
        subject = Subject(req.code, req.title, req.description, req.req_type, list(req.acceptance_criteria))
        for finding in check_requirement(subject):
            if finding.code == "NO_CRITERIA":
                evidence = req.title
                title = f"{req.code} has no acceptance criteria"
            elif finding.code == "PLACEHOLDER":
                evidence = next((u for u in units(req) if re.search(r"\b(tbd|tbc|todo|to be decided|to be confirmed)\b|\?\?\?", u, re.IGNORECASE)), req.title)
                title = f"{req.code} still has a placeholder"
            else:
                continue
            items.append(
                Item("risk", title, "Design built on an unfinished requirement is likely to change.", [req.code], evidence,
                     {"likelihood": "high", "impact": "medium", "mitigation": "Settle the requirement with its owner before relying on this part of the design."})
            )
    return Draft(items)


# --------------------------------------------------------------------------- grounding


def _near_duplicate(a: str, b: str) -> bool:
    ta, tb = content_tokens(a), content_tokens(b)
    if not ta or not tb:
        return a.strip().lower() == b.strip().lower()
    return len(ta & tb) / len(ta | tb) >= DUPLICATE_OVERLAP


@dataclass
class DesignReport:
    dropped: list[dict] = field(default_factory=list)

    def reasons(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.dropped:
            out[d["reason"]] = out.get(d["reason"], 0) + 1
        return out


def check_items(items: list[Item], reqs: list[ReqView], knowledge_text: str = "", known_components: set[str] | None = None) -> tuple[list[Item], DesignReport]:
    """Keep only elements a reviewer can trace back to the requirements.

    `known_components` holds names of components that already exist (approved earlier), so a flow may use them.
    """
    by_code = {r.code: r for r in reqs}
    report = DesignReport()
    kept: list[Item] = []
    for item in items:
        linked = [c for c in dict.fromkeys(item.req_codes) if c in by_code]
        if not linked:
            report.dropped.append({"title": item.title, "kind": item.kind, "reason": "no_requirement"})
            continue
        item.req_codes = linked
        source_text = " ".join(requirement_text(by_code[c]) for c in linked)
        if not item.evidence.strip():
            report.dropped.append({"title": item.title, "kind": item.kind, "reason": "no_evidence"})
            continue
        if not evidence_supported(item.evidence, f"{source_text} {knowledge_text}"):
            report.dropped.append({"title": item.title, "kind": item.kind, "reason": "evidence_not_in_requirement"})
            continue
        if any(k.kind == item.kind and _near_duplicate(k.title, item.title) for k in kept):
            report.dropped.append({"title": item.title, "kind": item.kind, "reason": "duplicate"})
            continue
        kept.append(item)

    names = {i.title for i in kept if i.kind == "component"} | (known_components or set())
    final: list[Item] = []
    for item in kept:
        if item.kind == "flow":
            ends = [item.detail.get("from_name", ""), item.detail.get("to_name", "")]
            external = bool(item.detail.get("to_external"))
            bad = [e for i, e in enumerate(ends) if e and e not in names and e != CLIENT and not (external and i == 1)]
            if bad:
                report.dropped.append({"title": item.title, "kind": "flow", "reason": "unknown_component", "names": bad})
                continue
        if item.kind == "component":
            item.detail["depends_on"] = [d for d in item.detail.get("depends_on", []) if d in names and d != item.title]
        final.append(item)
    return final, report
