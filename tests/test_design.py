"""Design pipeline: rules, grounding, providers, review, regeneration, coverage, export."""

import json
import re

import httpx
import pytest

from app.seed import DEMO_REQUIREMENTS
from app.services import agents as A
from app.services import llm
from app.services.design_rules import DesignReport, Item, check_items, draft_rules, severity
from app.services.grounding import evidence_supported, requirement_text

KEY = "sk-test-SECRET-1234567890"


def views(extra=()):
    rows = list(DEMO_REQUIREMENTS) + list(extra)
    return [A.ReqView(i, f"RQ-{i:03d}", t, d, ty, list(ac), 1) for i, (ty, t, d, ac) in enumerate(rows, start=1)]


def draft_api(client, headers, cid, **body):
    return client.post(f"/api/cycles/{cid}/design/generate", headers=headers, json=body)


def design(client, headers, cid, **params):
    return client.get(f"/api/cycles/{cid}/design", headers=headers, params=params).json()


def review(client, headers, cid, eid, status, notes=None):
    return client.patch(f"/api/cycles/{cid}/design/{eid}", headers=headers, json={"status": status, "review_notes": notes})


# ------------------------------------------------------------------ rules


def test_rules_draft_all_three_kinds_and_every_item_quotes_its_requirement():
    reqs = views()
    draft = draft_rules(reqs)
    kinds = {i.kind for i in draft.items}
    assert kinds == {"component", "flow", "risk"}
    by_code = {r.code: r for r in reqs}
    for item in draft.items:
        text = " ".join(requirement_text(by_code[c]) for c in item.req_codes)
        assert evidence_supported(item.evidence, text), (item.title, item.evidence)


def test_rules_find_the_expected_parts_in_the_demo_requirements():
    titles = {(i.kind, i.title) for i in draft_rules(views()).items}
    assert ("component", "Web UI") in titles
    assert ("component", "API layer") in titles
    assert ("component", "Checkout and payments") in titles
    assert ("flow", "POST /orders") in titles
    assert ("flow", "Checkout and payments to Payment provider") in titles
    assert ("risk", "Sensitive data exposed") in titles
    assert any(k == "risk" and t.startswith("Performance target may not hold") for k, t in titles)


def test_a_plain_word_like_email_in_a_login_requirement_is_not_a_notification_service():
    names = {i.title for i in draft_rules(views()).items}
    assert "Notifications" not in names and "Email service" not in " ".join(names)


def test_unfinished_requirements_become_risks():
    reqs = [
        A.ReqView(1, "RQ-001", "Export reports", "Details TBD", "Functional", [], 1),
    ]
    titles = {i.title for i in draft_rules(reqs).items if i.kind == "risk"}
    assert "RQ-001 has no acceptance criteria" in titles
    assert "RQ-001 still has a placeholder" in titles


def test_nothing_is_drafted_from_nothing():
    assert draft_rules([]).items == []


def test_severity_boundaries():
    assert severity("low", "low") == "low"
    assert severity("low", "medium") == "low"  # 2
    assert severity("low", "high") == "medium"  # 3
    assert severity("medium", "medium") == "medium"  # 4
    assert severity("medium", "high") == "high"  # 6
    assert severity("high", "high") == "high"  # 9
    assert severity("nonsense", "nonsense") == "medium"  # unknown values count as medium


# ------------------------------------------------------------------ grounding


def item(kind="component", title="Thing", codes=("RQ-001",), evidence="Registered users sign in with email and password", **detail):
    return Item(kind, title, "", list(codes), evidence, dict(detail))


def test_grounding_drops_what_a_reviewer_could_not_trace():
    reqs = views()
    items = [
        item(title="No code", codes=("RQ-999",)),
        item(title="No evidence", evidence=""),
        item(title="Made up evidence", evidence="The system shall mine cryptocurrency overnight"),
        item(title="Fine", evidence="Registered users sign in with email and password"),
        item(title="Fine", evidence="Registered users sign in with email and password"),  # duplicate
    ]
    kept, report = check_items(items, reqs)
    assert [i.title for i in kept] == ["Fine"]
    assert report.reasons() == {"no_requirement": 1, "no_evidence": 1, "evidence_not_in_requirement": 1, "duplicate": 1}


def test_grounding_checks_flow_ends_and_lets_outside_systems_through():
    reqs = views()
    ok = item("component", "Login service")
    unknown = item("flow", "Login service to Ghost", from_name="Login service", to_name="Ghost")
    external = item("flow", "Login service to Mailer", from_name="Login service", to_name="Mailer", to_external=True)
    client = item("flow", "Client to Login service", from_name="Client", to_name="Login service")
    kept, report = check_items([ok, unknown, external, client], reqs)
    assert {i.title for i in kept} == {"Login service", "Login service to Mailer", "Client to Login service"}
    assert report.dropped[0]["reason"] == "unknown_component"


def test_a_flow_may_use_a_component_that_was_approved_earlier():
    flow = item("flow", "A to B", from_name="Old part", to_name="Login service")
    kept, _ = check_items([item("component", "Login service"), flow], views(), known_components={"Old part"})
    assert len(kept) == 2


# ------------------------------------------------------------------ API and roles


def test_drafting_needs_requirements_and_a_qa_lead(client, lead, tester, viewer, cycle_id):
    assert draft_api(client, tester, cycle_id).status_code == 403
    assert draft_api(client, viewer, cycle_id).status_code == 403
    empty = client.post("/api/projects", headers=lead, json={"name": "Empty design"}).json()["cycles"][0]["id"]
    assert draft_api(client, lead, empty).status_code == 409
    assert client.get(f"/api/cycles/{cycle_id}/design", headers=viewer).status_code == 200


def test_draft_saves_numbered_elements_with_links(client, lead, cycle_id):
    res = draft_api(client, lead, cycle_id)
    assert res.status_code == 200, res.text
    summary = res.json()
    assert summary["generator"] == "Built-in rules"
    assert summary["total"] == sum(summary["created"].values()) > 10
    data = design(client, lead, cycle_id)
    codes = [e["code"] for e in data["elements"]]
    assert len(codes) == len(set(codes))
    assert all(re.fullmatch(r"(CMP|FLW|RSK)-\d{3}", c) for c in codes)
    assert all(e["status"] == "pending" and e["req_links"] for e in data["elements"])
    risk = next(e for e in data["elements"] if e["kind"] == "risk")
    assert risk["detail"]["severity"] in ("high", "medium", "low")
    flow = next(e for e in data["elements"] if e["title"] == "POST /orders")
    assert flow["detail"]["from_label"] == "Web UI" and flow["detail"]["to_label"] == "API layer"


def test_filtering_by_kind_and_status(client, lead, cycle_id):
    draft_api(client, lead, cycle_id)
    only_risks = design(client, lead, cycle_id, kind="risk")["elements"]
    assert only_risks and all(e["kind"] == "risk" for e in only_risks)
    assert design(client, lead, cycle_id, status="approved")["elements"] == []


def test_rejecting_needs_a_reason_and_approving_does_not(client, lead, tester, viewer, cycle_id):
    draft_api(client, lead, cycle_id)
    eid = design(client, lead, cycle_id)["elements"][0]["id"]
    assert review(client, tester, cycle_id, eid, "rejected").status_code == 422
    assert review(client, viewer, cycle_id, eid, "approved").status_code == 403
    ok = review(client, tester, cycle_id, eid, "approved")
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    rejected = review(client, tester, cycle_id, eid, "rejected", "We already have this")
    assert rejected.json()["review_notes"] == "We already have this"
    assert review(client, tester, cycle_id, 999999, "approved").status_code == 404


def test_bulk_review(client, lead, tester, cycle_id):
    draft_api(client, lead, cycle_id)
    ids = [e["id"] for e in design(client, lead, cycle_id)["elements"]][:4]
    res = client.patch(f"/api/cycles/{cycle_id}/design", headers=tester, json={"ids": ids, "status": "approved"})
    assert res.json() == {"updated": 4, "status": "approved"}
    assert client.patch(f"/api/cycles/{cycle_id}/design", headers=tester, json={"ids": [999999], "status": "approved"}).status_code == 404


def test_regenerating_keeps_approved_and_edited_and_never_reuses_a_code(client, lead, tester, cycle_id):
    draft_api(client, lead, cycle_id)
    first = design(client, lead, cycle_id)["elements"]
    approved = next(e for e in first if e["kind"] == "component")
    edited = next(e for e in first if e["kind"] == "risk")
    discarded = next(e for e in first if e["kind"] == "flow")
    review(client, tester, cycle_id, approved["id"], "approved")
    client.put(f"/api/cycles/{cycle_id}/design/{edited['id']}", headers=tester, json={"detail": {"mitigation": "Our own plan"}})
    highest = max(int(e["code"].split("-")[1]) for e in first if e["kind"] == "flow")

    summary = draft_api(client, lead, cycle_id).json()
    assert summary["kept"] == 2 and summary["already_approved"] >= 1
    after = design(client, lead, cycle_id)["elements"]
    by_code = {e["code"]: e for e in after}
    assert by_code[approved["code"]]["status"] == "approved"
    assert by_code[edited["code"]]["detail"]["mitigation"] == "Our own plan"
    assert discarded["code"] not in by_code  # pending and untouched, so replaced
    new_flows = [int(e["code"].split("-")[1]) for e in after if e["kind"] == "flow"]
    assert min(new_flows) > highest
    assert sum(1 for e in after if e["title"] == approved["title"]) == 1  # not proposed twice


def test_editing_a_risk_recomputes_severity_and_marks_it_corrected(client, lead, tester, cycle_id):
    draft_api(client, lead, cycle_id)
    risk = next(e for e in design(client, lead, cycle_id)["elements"] if e["kind"] == "risk")
    res = client.put(f"/api/cycles/{cycle_id}/design/{risk['id']}", headers=tester, json={"title": "Renamed", "detail": {"likelihood": "low", "impact": "low"}})
    body = res.json()
    assert body["title"] == "Renamed" and body["edited"] is True
    assert body["detail"]["severity"] == "low"
    bad = client.put(f"/api/cycles/{cycle_id}/design/{risk['id']}", headers=tester, json={"detail": {"impact": "extreme"}})
    assert bad.status_code == 422


def test_a_changed_requirement_makes_its_design_stale(client, lead, cycle_id):
    draft_api(client, lead, cycle_id)
    assert design(client, lead, cycle_id)["summary"]["stale"] == 0
    reqs = client.get(f"/api/cycles/{cycle_id}/requirements", headers=lead).json()
    target = next(r for r in reqs if r["title"] == "Search latency")
    res = client.patch(f"/api/cycles/{cycle_id}/requirements/{target['id']}", headers=lead, json={"description": "Search must stay fast: p95 under 500 ms at 200 users."})
    assert res.status_code == 200, res.text
    data = design(client, lead, cycle_id)
    stale = [e for e in data["elements"] if e["stale"]]
    assert stale and data["summary"]["stale"] == len(stale)


def test_requirements_with_no_design_are_listed_as_gaps(client, lead, cycle_id):
    draft_api(client, lead, cycle_id)
    before = design(client, lead, cycle_id)["summary"]
    client.post(f"/api/cycles/{cycle_id}/requirements", headers=lead, json={"title": "Zebra", "description": "Zzz", "req_type": "Performance", "acceptance_criteria": ["Be quick"]})
    after = design(client, lead, cycle_id)["summary"]
    assert len(after["gaps"]) == len(before["gaps"]) + 1
    assert any(g["title"] == "Zebra" for g in after["gaps"])
    assert after["covered"] == before["covered"]


def test_rejected_elements_do_not_count_as_coverage(client, lead, tester, cycle_id):
    draft_api(client, lead, cycle_id)
    data = design(client, lead, cycle_id)
    only = next(e for e in data["elements"] if e["title"] == "GET /products/{id}")
    code = only["req_links"][0]["code"]
    assert code not in [g["code"] for g in data["summary"]["gaps"]]
    for e in data["elements"]:
        if any(l["code"] == code for l in e["req_links"]):
            review(client, tester, cycle_id, e["id"], "rejected", "Not needed")
    assert code in [g["code"] for g in design(client, lead, cycle_id)["summary"]["gaps"]]


# ------------------------------------------------------------------ export and traceability


def test_export_needs_an_approved_element_and_only_contains_approved_ones(client, lead, tester, viewer, cycle_id):
    draft_api(client, lead, cycle_id)
    url = f"/api/cycles/{cycle_id}/design/export/md"
    assert client.get(url, headers=lead).status_code == 409
    assert client.get(url, headers=tester).status_code == 403
    elements = design(client, lead, cycle_id)["elements"]
    chosen = [e for e in elements if e["kind"] == "component" and e["title"] in ("Web UI", "API layer")]
    chosen += [e for e in elements if e["title"] == "GET /products/{id}"]
    chosen += [e for e in elements if e["kind"] == "risk"][:1]
    client.patch(f"/api/cycles/{cycle_id}/design", headers=tester, json={"ids": [e["id"] for e in chosen], "status": "approved"})
    res = client.get(url, headers=lead)
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/markdown")
    text = res.text
    assert "```mermaid" in text and "flowchart LR" in text
    assert "## Components" in text and "## Risk register" in text and "## Requirement to design" in text
    assert "| Web UI |" in text and "API layer" in text
    assert "Checkout and payments" not in text  # not approved
    assert "not covered" in text


def test_export_escapes_table_cells(client, lead, tester, cycle_id):
    draft_api(client, lead, cycle_id)
    comp = next(e for e in design(client, lead, cycle_id)["elements"] if e["kind"] == "component")
    client.put(f"/api/cycles/{cycle_id}/design/{comp['id']}", headers=tester, json={"title": "A | B\nC", "detail": {"responsibility": "x | y"}})
    review(client, tester, cycle_id, comp["id"], "approved")
    text = client.get(f"/api/cycles/{cycle_id}/design/export/md", headers=lead).text
    assert "A \\| B C" in text and "x \\| y" in text


def test_traceability_and_summary_carry_the_design(client, lead, tester, cycle_id):
    draft_api(client, lead, cycle_id)
    elements = design(client, lead, cycle_id)["elements"]
    api = next(e for e in elements if e["title"] == "API layer")
    review(client, tester, cycle_id, api["id"], "approved")
    rows = client.get(f"/api/cycles/{cycle_id}/traceability", headers=lead).json()["rows"]
    linked = {l["code"] for l in api["req_links"]}
    assert {r["code"] for r in rows if api["code"] in r["design"]} == linked
    assert all(r["design"] == [] for r in rows if r["code"] not in linked)
    summary = client.get(f"/api/cycles/{cycle_id}", headers=lead).json()["summary"]
    assert summary["design"]["approved"] == 1 and summary["design"]["elements"] > 1


# ------------------------------------------------------------------ providers


def design_payload(code, quote):
    return {
        "components": [
            {"name": "Sign-in service", "responsibility": "Checks credentials", "interfaces": ["POST /login"], "depends_on": ["Account store"], "req_codes": [code], "evidence": quote},
            {"name": "Account store", "responsibility": "Keeps accounts", "req_codes": [code], "evidence": quote},
            {"name": "Invented part", "responsibility": "Not in any requirement", "req_codes": [code], "evidence": "Quantum teleportation of user sessions"},
        ],
        "flows": [
            {"from": "Sign-in service", "to": "Account store", "data": "Credentials", "trigger": "A user signs in", "req_codes": [code], "evidence": quote},
            {"from": "Sign-in service", "to": "Ghost", "data": "x", "req_codes": [code], "evidence": quote},
        ],
        "risks": [
            {"title": "Credential stuffing", "description": "Repeated guesses", "likelihood": "high", "impact": "high", "mitigation": "Lock accounts", "req_codes": [code], "evidence": quote},
        ],
        "clarifications": [{"req_code": code, "question": "How long is a lockout?"}, {"req_code": "RQ-404", "question": "ignored"}],
    }


def install(monkeypatch, handler):
    monkeypatch.setattr(llm, "http_factory", lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def first_requirement(prompt):
    code = re.search(r"RQ-\d+", prompt).group(0)
    quote = re.search(r'"title": "([^"]+)"', prompt).group(1)
    return code, quote


def model(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((str(request.url), dict(request.headers), body))
        if "anthropic" in str(request.url):
            code, quote = first_requirement(body["messages"][0]["content"])
            block = {"type": "tool_use", "name": "submit_design", "input": design_payload(code, quote)}
            return httpx.Response(200, json={"stop_reason": "tool_use", "content": [block]})
        code, quote = first_requirement(body["messages"][1]["content"])
        call = {"function": {"name": "submit_design", "arguments": json.dumps(design_payload(code, quote))}}
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [call]}}]})

    return handler


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_both_providers_draft_a_design_and_ungrounded_parts_are_dropped(client, lead, cycle_id, monkeypatch, provider):
    seen = []
    install(monkeypatch, model(seen))
    res = draft_api(client, lead, cycle_id, provider=provider, api_key=KEY)
    assert res.status_code == 200, res.text
    summary = res.json()
    assert summary["created"] == {"component": 2, "flow": 1, "risk": 1}
    assert summary["dropped"] == 2 and summary["dropped_reasons"] == {"evidence_not_in_requirement": 1, "unknown_component": 1}
    assert summary["clarifications"] == [{"req_code": summary["clarifications"][0]["req_code"], "question": "How long is a lockout?"}]
    assert summary["fallback_reason"] is None
    assert KEY not in res.text
    # the tool the model was forced to call is the design tool, and the key went in the header only
    body = seen[0][2]
    forced = body.get("tool_choice", {})
    assert forced.get("name") == "submit_design" or forced.get("function", {}).get("name") == "submit_design"
    data = design(client, lead, cycle_id)["elements"]
    assert all(e["source"] == provider for e in data)
    comp = next(e for e in data if e["title"] == "Sign-in service")
    assert [d["name"] for d in comp["detail"]["depends_on_names"]] == ["Account store"]
    assert KEY not in json.dumps(data)


def test_a_failing_provider_falls_back_to_rules_and_never_shows_the_key(client, lead, cycle_id, monkeypatch):
    install(monkeypatch, lambda request: httpx.Response(500, json={"error": {"message": f"boom {KEY}"}}))
    res = draft_api(client, lead, cycle_id, provider="anthropic", api_key=KEY)
    assert res.status_code == 200
    summary = res.json()
    assert summary["generator"] == "Built-in rules (fallback)"
    assert summary["fallback_reason"] and KEY not in res.text
    assert summary["total"] > 10
    assert {e["source"] for e in design(client, lead, cycle_id)["elements"]} == {"rules-fallback"}


def test_a_model_that_ignores_the_tool_also_falls_back(client, lead, cycle_id, monkeypatch):
    install(monkeypatch, lambda request: httpx.Response(200, json={"stop_reason": "end_turn", "content": [{"type": "text", "text": "Sure!"}]}))
    summary = draft_api(client, lead, cycle_id, provider="anthropic", api_key=KEY).json()
    assert "expected format" in summary["fallback_reason"]


def test_a_provider_needs_a_key(client, lead, cycle_id):
    res = draft_api(client, lead, cycle_id, provider="openai")
    assert res.status_code == 422 and "API key" in res.text


def test_reviewer_feedback_reaches_the_next_prompt(client, lead, tester, cycle_id, monkeypatch):
    draft_api(client, lead, cycle_id)
    elements = design(client, lead, cycle_id)["elements"]
    keep = next(e for e in elements if e["title"] == "Web UI")
    drop = next(e for e in elements if e["kind"] == "risk")
    review(client, tester, cycle_id, keep["id"], "approved")
    review(client, tester, cycle_id, drop["id"], "rejected", "Too generic for this team")
    seen = []
    install(monkeypatch, model(seen))
    draft_api(client, lead, cycle_id, provider="anthropic", api_key=KEY)
    prompt = seen[0][2]["messages"][0]["content"]
    assert "Web UI" in prompt and "Too generic for this team" in prompt


def test_the_empty_draft_summary_has_no_leftover_state(client, lead, cycle_id):
    first = draft_api(client, lead, cycle_id).json()
    second = draft_api(client, lead, cycle_id).json()
    assert second["total"] == first["total"] and second["kept"] == 0
    assert isinstance(DesignReport().reasons(), dict)


def test_diagram_labels_are_never_cut_mid_word():
    from app.services.design import _edge_label, _short

    assert _short("short") == "short"
    cut = _short("A very long label that keeps going and going beyond the limit")
    assert cut.endswith("...") and not cut[:-3].endswith(("goi", "go")) and len(cut) <= 40
    assert _edge_label("Checkout and payments to Payment provider", "Takes card payments", "Checkout and payments") == "Takes card payments"
    assert _edge_label("POST /orders", "POST /orders creates an order", "Web UI") == "POST /orders"
