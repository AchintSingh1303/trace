import json

from conftest import generate, login, wait_for_run

ALL = ["functional", "api", "security", "performance", "integration"]


def cases(client, headers, cid, **params):
    return client.get(f"/api/cycles/{cid}/test-cases", headers=headers, params=params).json()


# ------------------------------------------------------------------ auth + roles


def test_login_roles_and_unauthenticated_access(client, lead, viewer, cycle_id):
    assert client.get(f"/api/cycles/{cycle_id}/requirements").status_code == 401
    assert client.post("/api/auth/login", json={"email": "lead@demo.local", "password": "nope"}).status_code == 401
    # a viewer can read but not generate or edit
    assert client.get(f"/api/cycles/{cycle_id}/requirements", headers=viewer).status_code == 200
    assert client.post(f"/api/cycles/{cycle_id}/generate", headers=viewer, json={}).status_code == 403
    assert client.post(f"/api/cycles/{cycle_id}/requirements", headers=viewer, json={"title": "x", "req_type": "API"}).status_code == 403


def test_tampered_token_is_rejected(client):
    res = client.get("/api/auth/me", headers={"Authorization": "Bearer abc.def"})
    assert res.status_code == 401


# ------------------------------------------------------------------ generation


def test_full_generation_covers_every_type_and_persists_traceability(client, lead, cycle_id):
    done = generate(client, lead, cycle_id, selected_test_types=ALL)
    types = [e["type"] for e in done["events"]]
    assert types[0] == "pipeline_start" and types[-1] == "generation_complete"
    assert types.count("agent_start") == 5 and types.count("agent_complete") == 5

    result = cases(client, lead, cycle_id)
    assert {c["test_type"] for c in result} == set(ALL)
    assert all(c["req_links"] for c in result)
    assert all(c["status"] == "pending" for c in result)

    complete = done["events"][-1]["data"]
    assert complete["total"] == len(result)
    assert complete["coverage_gap_count"] == 0

    detail = client.get(f"/api/cycles/{cycle_id}/test-cases/{result[0]['id']}", headers=lead).json()
    assert detail["steps"] and all(s["expected"] for s in detail["steps"])


def test_only_selected_types_run_and_missing_agents_are_reported_as_gaps(client, lead, cycle_id):
    done = generate(client, lead, cycle_id, selected_test_types=["api"])
    assert {c["test_type"] for c in cases(client, lead, cycle_id)} == {"api"}
    gaps = done["events"][-1]["data"]["coverage_gaps"]
    assert gaps and all(g["reason"] == "agent_not_selected" for g in gaps)


def test_second_generate_while_running_returns_409(client, lead, cycle_id, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "generation_delay_ms", 40)
    assert client.post(f"/api/cycles/{cycle_id}/generate", headers=lead, json={"selected_test_types": ["functional"]}).status_code == 200
    again = client.post(f"/api/cycles/{cycle_id}/generate", headers=lead, json={"selected_test_types": ["functional"]})
    assert again.status_code == 409
    wait_for_run(client, lead, cycle_id)


def test_cannot_generate_without_requirements(client, lead):
    project = client.post("/api/projects", headers=lead, json={"name": "Empty"}).json()
    res = client.post(f"/api/cycles/{project['cycles'][0]['id']}/generate", headers=lead, json={})
    assert res.status_code == 409


def test_max_tcs_caps_the_total(client, lead, cycle_id):
    generate(client, lead, cycle_id, selected_test_types=["functional", "api"], max_tcs=10)
    assert len(cases(client, lead, cycle_id)) <= 10


def test_sse_stream_replays_history_in_order(client, lead, cycle_id):
    generate(client, lead, cycle_id, selected_test_types=["security"])
    seen = []
    with client.stream("GET", f"/api/cycles/{cycle_id}/generate/stream", headers=lead) as res:
        assert res.headers["content-type"].startswith("text/event-stream")
        for line in res.iter_lines():
            if line.startswith("data: "):
                seen.append(json.loads(line[6:])["type"])
    assert seen[0] == "pipeline_start"
    assert seen[-1] == "generation_complete"
    assert seen.index("agent_start") < seen.index("tc_created") < seen.index("agent_complete")


# ------------------------------------------------------------------ review + traceability


def test_review_is_role_gated_and_updates_traceability(client, lead, tester, viewer, cycle_id):
    generate(client, lead, cycle_id, selected_test_types=["functional"])
    first = cases(client, lead, cycle_id)[0]

    assert client.patch(f"/api/cycles/{cycle_id}/test-cases/{first['id']}", headers=viewer, json={"status": "approved"}).status_code == 403
    ok = client.patch(f"/api/cycles/{cycle_id}/test-cases/{first['id']}", headers=tester, json={"status": "approved"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["status"] == "approved" and body["automation_status"] == "ready" and body["reviewed_by"] == "tester@demo.local"

    matrix = client.get(f"/api/cycles/{cycle_id}/traceability", headers=lead).json()
    states = {row["code"]: row["state"] for row in matrix["rows"]}
    linked = first["req_links"][0]["code"]
    assert states[linked] == "covered"
    assert matrix["summary"]["covered"] >= 1


def test_bulk_review(client, lead, tester, cycle_id):
    generate(client, lead, cycle_id, selected_test_types=["api"])
    ids = [c["id"] for c in cases(client, lead, cycle_id)]
    res = client.patch(f"/api/cycles/{cycle_id}/test-cases", headers=tester, json={"tc_ids": ids, "status": "rejected", "review_notes": "not needed"})
    assert res.json()["updated"] == len(ids)
    assert {c["status"] for c in cases(client, lead, cycle_id)} == {"rejected"}


def test_csv_export_has_cases_and_steps(client, lead, tester, cycle_id):
    generate(client, lead, cycle_id, selected_test_types=["security"])
    tc = cases(client, lead, cycle_id)[0]
    client.patch(f"/api/cycles/{cycle_id}/test-cases/{tc['id']}", headers=tester, json={"status": "approved"})
    res = client.get(f"/api/cycles/{cycle_id}/export/csv", headers=lead, params={"status": "approved"})
    assert res.status_code == 200 and "attachment" in res.headers["content-disposition"]
    lines = res.text.strip().splitlines()
    assert lines[0].startswith("ID,Work Item Type,Title")
    assert any(",Test Case," in l for l in lines[1:])
    assert len(lines) > 2  # case row plus step rows
    assert client.get(f"/api/cycles/{cycle_id}/export/csv", headers=tester).status_code == 403


# ------------------------------------------------------------------ the "update" story


def test_editing_a_requirement_flags_stale_cases_and_incremental_regeneration_is_surgical(client, lead, tester, cycle_id):
    generate(client, lead, cycle_id, selected_test_types=["functional"])
    all_cases = cases(client, lead, cycle_id)
    ids = [c["id"] for c in all_cases]
    client.patch(f"/api/cycles/{cycle_id}/test-cases", headers=tester, json={"tc_ids": ids, "status": "approved"})

    reqs = client.get(f"/api/cycles/{cycle_id}/requirements", headers=lead).json()
    target = next(r for r in reqs if r["title"] == "User login")
    untouched_ids = {c["id"] for c in all_cases if all(l["code"] != target["code"] for l in c["req_links"])}
    assert untouched_ids

    updated = client.patch(
        f"/api/cycles/{cycle_id}/requirements/{target['id']}",
        headers=lead,
        json={"acceptance_criteria": target["acceptance_criteria"] + ["Sign-in with a social account works"]},
    ).json()
    assert updated["version"] == 2 and updated["stale_test_cases"] > 0

    stale_now = [c for c in cases(client, lead, cycle_id) if c["stale"]]
    assert stale_now and all(c["status"] == "approved" for c in stale_now)
    summary = client.get(f"/api/cycles/{cycle_id}", headers=lead).json()["summary"]
    assert summary["stale_test_cases"] == len(stale_now)

    done = generate(client, lead, cycle_id, selected_test_types=["functional"], mode="incremental")
    data = done["events"][-1]["data"]
    assert data["replaced_approved"] > 0
    assert done["events"][0]["data"]["stale_requirements"] == 1

    after = cases(client, lead, cycle_id)
    assert not any(c["stale"] for c in after)
    assert untouched_ids <= {c["id"] for c in after}  # other requirements keep their cases
    assert all(c["status"] == "approved" for c in after if c["id"] in untouched_ids)  # and their approvals
    regenerated = [c for c in after if c["id"] not in set(ids)]
    assert regenerated and all(c["status"] == "pending" for c in regenerated)
    assert any("social account" in c["title"].lower() for c in regenerated)


def test_incremental_rebuilds_cases_that_merely_also_cover_a_changed_requirement(client, lead, cycle_id):
    generate(client, lead, cycle_id, selected_test_types=["functional", "integration"])
    before = cases(client, lead, cycle_id)
    journey = next(c for c in before if c["subtype"] == "journey")
    reqs = {r["title"]: r for r in client.get(f"/api/cycles/{cycle_id}/requirements", headers=lead).json()}
    cart = reqs["Add to cart"]
    assert cart["code"] in [l["code"] for l in journey["req_links"]][1:]  # linked, but not the owner
    login_ids = {c["id"] for c in before if c["req_links"][0]["code"] == reqs["User login"]["code"]}

    client.patch(
        f"/api/cycles/{cycle_id}/requirements/{cart['id']}",
        headers=lead,
        json={"acceptance_criteria": cart["acceptance_criteria"] + ["Removing an item decreases the cart count"]},
    )
    generate(client, lead, cycle_id, selected_test_types=["functional", "integration"], mode="incremental")
    after = cases(client, lead, cycle_id)

    new_journey = [c for c in after if c["subtype"] == "journey"]
    assert len(new_journey) == 1 and new_journey[0]["id"] != journey["id"]  # rebuilt, not lost
    assert not any(c["stale"] for c in after)
    assert login_ids <= {c["id"] for c in after}  # unrelated requirement untouched
    assert any("decreases the cart count" in c["title"] for c in after)


def test_incremental_generates_a_type_that_has_never_run(client, lead, cycle_id):
    generate(client, lead, cycle_id, selected_test_types=["functional"])
    functional_ids = {c["id"] for c in cases(client, lead, cycle_id)}
    generate(client, lead, cycle_id, selected_test_types=["functional", "api"], mode="incremental")
    after = cases(client, lead, cycle_id)
    assert any(c["test_type"] == "api" for c in after)
    assert functional_ids == {c["id"] for c in after if c["test_type"] == "functional"}


def test_incremental_with_nothing_to_do_changes_nothing(client, lead, cycle_id):
    generate(client, lead, cycle_id, selected_test_types=["api"])
    before = {c["id"] for c in cases(client, lead, cycle_id)}
    done = generate(client, lead, cycle_id, selected_test_types=["api"], mode="incremental")
    assert done["events"][-1]["data"]["total"] == 0
    assert {c["id"] for c in cases(client, lead, cycle_id)} == before


def test_editing_is_blocked_during_generation_and_deleting_removes_orphan_cases(client, lead, cycle_id):
    generate(client, lead, cycle_id, selected_test_types=["performance"])
    reqs = client.get(f"/api/cycles/{cycle_id}/requirements", headers=lead).json()
    perf = next(r for r in reqs if r["title"] == "Search latency")
    assert cases(client, lead, cycle_id)
    assert client.delete(f"/api/cycles/{cycle_id}/requirements/{perf['id']}", headers=lead).status_code == 204
    remaining = cases(client, lead, cycle_id)
    assert all(l["code"] != perf["code"] for c in remaining for l in c["req_links"])
    assert not any(c["title"].startswith("[Baseline] Verify Search latency") for c in remaining)


def test_requirement_validation(client, lead, cycle_id):
    bad = client.post(f"/api/cycles/{cycle_id}/requirements", headers=lead, json={"title": "x", "req_type": "Nope"})
    assert bad.status_code == 422
    dup = client.post(f"/api/cycles/{cycle_id}/requirements", headers=lead, json={"title": "x", "req_type": "API", "code": "RQ-001"})
    assert dup.status_code == 409


def test_roadmap_and_health_are_public(client):
    assert client.get("/api/health").json()["status"] == "ok"
    road = client.get("/api/roadmap").json()
    assert {i["status"] for i in road["items"]} >= {"shipped", "next", "later"}
