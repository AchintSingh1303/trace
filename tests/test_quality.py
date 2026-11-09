"""Grounding, project knowledge, feedback from review, evaluation, import and user management."""

import json
import re
import time

import httpx
import pytest

from app.services import agents as A
from app.services import grounding, llm
from app.services.evaluation import rules_generator, score
from app.services.importer import parse_requirements

from conftest import generate, login, wait_for_run

KEY = "sk-test-SECRET-1234567890"
REQ = A.ReqView(1, "RQ-001", "Borrow a book", "A member borrows a book for 14 days.", "Functional", ["A member cannot borrow more than 5 books"], 1)


def case(title="[Happy Path] Verify borrow", evidence="A member borrows a book for 14 days", steps=None, codes=("RQ-001",)):
    return A.Case(title, "functional", "happy_path", "high", steps or [{"action": "Borrow a book", "expected": "Due in 14 days"}], list(codes), evidence=evidence)


# ------------------------------------------------------------------ grounding unit tests


def test_evidence_must_be_quoted_from_the_requirement():
    kept, report = grounding.check_cases(
        [case(), case("[Negative] Verify fee", evidence="Late returns are charged 2 dollars a day"), case("[Smoke] Verify x", evidence="")],
        [REQ],
    )
    assert [c.title for c in kept] == ["[Happy Path] Verify borrow"]
    assert report.reasons() == {"evidence_not_in_requirement": 1, "no_evidence": 1}


def test_evidence_survives_case_and_punctuation_differences():
    assert grounding.evidence_supported("a member CANNOT borrow more than 5 books.", "A member cannot borrow more than 5 books")


def test_invented_endpoints_and_fields_are_flagged_or_dropped_when_strict():
    invented = case(steps=[{"action": "Call POST /api/v2/loans with member_id", "expected": "Created"}])
    kept, report = grounding.check_cases([invented], [REQ])
    assert kept and "member_id" in kept[0].warnings[0] and "/api/v2/loans" in kept[0].warnings[0] and report.warned == 1

    strict_kept, strict_report = grounding.check_cases([case(steps=invented.steps)], [REQ], strict=True)
    assert not strict_kept and strict_report.reasons() == {"invented_terms": 1}


def test_terms_from_project_knowledge_are_allowed():
    c = case(steps=[{"action": "Call POST /loans with member_id", "expected": "Created"}])
    kept, _ = grounding.check_cases([c], [REQ], knowledge_text="Loans are created with POST /loans using member_id")
    assert kept and not kept[0].warnings


def test_near_duplicates_are_collapsed():
    kept, report = grounding.check_cases([case(), case("[Happy Path] Verify borrow.")], [REQ])
    assert len(kept) == 1 and report.reasons() == {"duplicate": 1}


# ------------------------------------------------------------------ end to end with a model that makes things up


def lying_model(monkeypatch, prompts):
    """A fake model that returns one honest case and one invention for every request."""

    def handler(request):
        body = json.loads(request.content)
        prompt = body["messages"][0]["content"] if "anthropic" in str(request.url) else body["messages"][1]["content"]
        prompts.append(prompt)
        code = re.search(r"RQ-\d+", prompt).group(0)
        title = re.search(r'"title": "([^"]+)"', prompt).group(1)
        payload = {
            "test_cases": [
                {"title": "[Happy Path] Verify honest case", "priority": "high", "req_codes": [code], "evidence": title,
                 "steps": [{"action": "Do it", "expected": "Works"}]},
                {"title": "[Negative] Verify invented behaviour", "priority": "high", "req_codes": [code],
                 "evidence": "The system emails a receipt within 5 seconds",
                 "steps": [{"action": "Wait for the receipt email", "expected": "Email arrives"}]},
            ],
            "clarifications": [{"req_code": code, "question": "What is the maximum allowed length?"}, {"req_code": "RQ-404", "question": "ignored"}],
        }
        if "anthropic" in str(request.url):
            return httpx.Response(200, json={"stop_reason": "tool_use", "content": [{"type": "tool_use", "name": llm.TOOL_NAME, "input": payload}]})
        call = {"function": {"name": llm.TOOL_NAME, "arguments": json.dumps(payload)}}
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [call]}}]})

    monkeypatch.setattr(llm, "http_factory", lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def test_invented_cases_never_reach_the_review_queue(client, lead, cycle_id, monkeypatch):
    prompts = []
    lying_model(monkeypatch, prompts)
    res = client.post(f"/api/cycles/{cycle_id}/generate", headers=lead,
                      json={"selected_test_types": ["functional"], "provider": "anthropic", "api_key": KEY})
    assert res.status_code == 200
    done = wait_for_run(client, lead, cycle_id)
    complete = done["events"][-1]["data"]
    assert complete["dropped"] >= 1 and complete["clarifications"]
    assert all(c["req_code"] != "RQ-404" for c in complete["clarifications"])  # unknown codes are ignored
    assert any(e["type"] == "agent_warning" and "not grounded" in e["data"]["message"] for e in done["events"])

    stored = client.get(f"/api/cycles/{cycle_id}/test-cases", headers=lead).json()
    assert stored and all("invented" not in c["title"] for c in stored)
    detail = client.get(f"/api/cycles/{cycle_id}/test-cases/{stored[0]['id']}", headers=lead).json()
    assert detail["evidence"] and detail["source"] == "anthropic"


# ------------------------------------------------------------------ project knowledge and feedback


def project_of(client, lead, cid):
    for p in client.get("/api/projects", headers=lead).json():
        if any(c["id"] == cid for c in p["cycles"]):
            return p["id"]


def test_knowledge_is_role_gated_and_reaches_the_prompt(client, lead, tester, viewer, cycle_id, monkeypatch):
    pid = project_of(client, lead, cycle_id)
    url = f"/api/projects/{pid}/knowledge"
    body = {"kind": "rule", "title": "Card lock", "content": "Cards lock after 5 failed sign-ins and unlock after 30 minutes"}
    assert client.post(url, headers=tester, json=body).status_code == 403
    created = client.post(url, headers=lead, json=body)
    assert created.status_code == 201
    assert client.post(url, headers=lead, json={**body, "kind": "nonsense"}).status_code == 422
    assert len(client.get(url, headers=viewer).json()) == 1

    prompts = []
    lying_model(monkeypatch, prompts)
    client.post(f"/api/cycles/{cycle_id}/generate", headers=lead, json={"selected_test_types": ["functional"], "provider": "openai", "api_key": KEY})
    done = wait_for_run(client, lead, cycle_id)
    assert done["events"][0]["data"]["knowledge_items"] == 1
    assert "Project knowledge (authoritative)" in prompts[0] and "unlock after 30 minutes" in prompts[0]

    assert client.delete(f"{url}/{created.json()['id']}", headers=lead).status_code == 204
    assert client.get(url, headers=lead).json() == []


def test_review_feedback_is_fed_back_into_the_next_prompt(client, lead, tester, cycle_id, monkeypatch):
    generate(client, lead, cycle_id, selected_test_types=["functional"])
    cases = client.get(f"/api/cycles/{cycle_id}/test-cases", headers=lead).json()
    good, bad = cases[0], cases[1]

    edited = client.put(
        f"/api/cycles/{cycle_id}/test-cases/{good['id']}", headers=tester,
        json={"title": "[Happy Path] Verify that a corrected title is used", "steps": [{"action": "Open the page", "expected": "It loads"}]},
    )
    assert edited.status_code == 200 and edited.json()["edited"] is True and edited.json()["steps"][0]["action"] == "Open the page"
    client.patch(f"/api/cycles/{cycle_id}/test-cases/{good['id']}", headers=tester, json={"status": "approved"})
    client.patch(f"/api/cycles/{cycle_id}/test-cases/{bad['id']}", headers=tester, json={"status": "rejected", "review_notes": "Never assume a guest checkout exists"})

    prompts = []
    lying_model(monkeypatch, prompts)
    client.post(f"/api/cycles/{cycle_id}/generate", headers=lead, json={"selected_test_types": ["functional"], "provider": "anthropic", "api_key": KEY})
    wait_for_run(client, lead, cycle_id)
    prompt = prompts[0]
    assert "corrected title is used" in prompt  # the human-corrected approved case is a style example
    assert "Never assume a guest checkout exists" in prompt  # the rejection reason is a warning


def test_a_case_needs_a_step_to_be_saved_and_viewers_cannot_edit(client, lead, viewer, cycle_id):
    generate(client, lead, cycle_id, selected_test_types=["api"])
    tc = client.get(f"/api/cycles/{cycle_id}/test-cases", headers=lead).json()[0]
    url = f"/api/cycles/{cycle_id}/test-cases/{tc['id']}"
    assert client.put(url, headers=viewer, json={"title": "x"}).status_code == 403
    assert client.put(url, headers=lead, json={"steps": [{"action": "  ", "expected": ""}]}).status_code == 422


# ------------------------------------------------------------------ import, users, evaluation


def test_csv_import_reports_bad_rows_and_keeps_the_good_ones(client, lead, tester, cycle_id):
    project = client.post("/api/projects", headers=lead, json={"name": f"Import {time.time_ns()}"}).json()
    cid = project["cycles"][0]["id"]
    text = (
        "code,type,title,description,acceptance_criteria\n"
        'RQ-010,Functional,Reset password,Members reset via email,"Link expires after 1 hour|Old password stops working"\n'
        ",api,GET /status,,\n"
        ",Banana,Bad type,,\n"
        ",Functional,,,\n"
    )
    assert client.post(f"/api/cycles/{cid}/requirements/import", headers=tester, json={"csv": text}).status_code == 403
    res = client.post(f"/api/cycles/{cid}/requirements/import", headers=lead, json={"csv": text}).json()
    assert res["created"] == 2 and res["codes"][0] == "RQ-010"
    assert sorted(e["line"] for e in res["errors"]) == [4, 5]
    reqs = client.get(f"/api/cycles/{cid}/requirements", headers=lead).json()
    assert reqs[0]["acceptance_criteria"] == ["Link expires after 1 hour", "Old password stops working"]
    assert {r["req_type"] for r in reqs} == {"Functional", "API"}
    again = client.post(f"/api/cycles/{cid}/requirements/import", headers=lead, json={"csv": text}).json()
    assert any("already exists" in e["error"] for e in again["errors"])


def test_import_rejects_files_without_a_title_column():
    rows, errors = parse_requirements("name,type\nx,API\n")
    assert not rows and "title" in errors[0]["error"]


def test_admin_manages_users_and_keeps_one_admin(client, lead):
    admin = login(client, "admin@demo.local")
    body = {"email": "New.Person@example.com", "name": "New Person", "role": "tester", "password": "long-enough-1"}
    assert client.post("/api/users", headers=lead, json=body).status_code == 403
    created = client.post("/api/users", headers=admin, json=body)
    assert created.status_code == 201 and created.json()["email"] == "new.person@example.com"
    assert client.post("/api/users", headers=admin, json=body).status_code == 409
    assert client.post("/api/users", headers=admin, json={**body, "email": "x@example.com", "password": "short"}).status_code == 422

    uid = created.json()["id"]
    assert client.patch(f"/api/users/{uid}", headers=admin, json={"role": "qa_lead", "password": "another-pass-2"}).json()["role"] == "qa_lead"
    assert client.post("/api/auth/login", json={"email": "new.person@example.com", "password": "another-pass-2"}).status_code == 200

    me = client.get("/api/auth/me", headers=admin).json()
    assert client.delete(f"/api/users/{me['id']}", headers=admin).status_code == 409
    assert client.patch(f"/api/users/{me['id']}", headers=admin, json={"role": "viewer"}).status_code == 409
    assert client.delete(f"/api/users/{uid}", headers=admin).status_code == 204


def test_public_config_and_demo_account_listing(client):
    assert client.get("/api/config").json()["demo"] is True
    assert client.get("/api/auth/demo-users").json()


GOLDEN = {
    "project": {"name": "P"},
    "requirements": [
        {"code": "RQ-001", "agent": "functional", "type": "Functional", "title": "Borrow a book", "description": "A member borrows a book for 14 days.",
         "acceptance_criteria": ["A member cannot borrow more than 5 books"], "must_cover": ["5 books"], "forbidden": ["late fee"]},
    ],
    "thresholds": {"criteria_coverage": 0.8, "must_cover": 0.8, "max_forbidden": 0},
}


def test_evaluation_passes_good_output_and_fails_invented_or_missing_content():
    assert score(GOLDEN, rules_generator)["passed"]

    def inventive(agent, targets, project):
        c = A.Case("[Negative] Verify the late fee is charged", agent, "negative", "high", [{"action": "Return late", "expected": "A late fee applies"}], ["RQ-001"])
        return [c], 0, 0

    result = score(GOLDEN, inventive)
    assert not result["passed"] and result["forbidden_hits"] == 1 and any("forbidden" in f for f in result["failures"])


def test_older_databases_gain_the_new_columns(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect, text

    from app import db as dbmod

    engine = create_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE test_cases (id INTEGER PRIMARY KEY, title VARCHAR(500))"))
    monkeypatch.setattr(dbmod, "engine", engine)
    dbmod._add_missing_columns()
    assert {"evidence", "warnings", "source", "edited"} <= {c["name"] for c in inspect(engine).get_columns("test_cases")}
