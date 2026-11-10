from conftest import generate


def approve_some(client, lead, tester, cid, test_type="api", limit=None):
    cases = client.get(f"/api/cycles/{cid}/test-cases", headers=lead, params={"test_type": test_type}).json()
    ids = [c["id"] for c in cases][:limit]
    res = client.patch(f"/api/cycles/{cid}/test-cases", headers=tester, json={"tc_ids": ids, "status": "approved"})
    assert res.status_code == 200, res.text
    return ids


def prepared(client, lead, tester, cid, limit=4):
    generate(client, lead, cid, test_types=["api"])
    return approve_some(client, lead, tester, cid, limit=limit)


def result(client, headers, cid, run_id, tc_id, status, **extra):
    return client.put(f"/api/cycles/{cid}/runs/{run_id}/results/{tc_id}", headers=headers, json={"status": status, **extra})


def test_run_needs_approved_cases_and_a_lead(client, lead, tester, cycle_id):
    res = client.post(f"/api/cycles/{cycle_id}/runs", headers=lead, json={"name": "Smoke"})
    assert res.status_code == 409
    prepared(client, lead, tester, cycle_id)
    assert client.post(f"/api/cycles/{cycle_id}/runs", headers=tester, json={"name": "Smoke"}).status_code == 403
    res = client.post(f"/api/cycles/{cycle_id}/runs", headers=lead, json={"name": "Smoke"})
    assert res.status_code == 201
    body = res.json()
    assert body["total"] == 4 and body["not_run"] == 4 and body["pass_rate"] is None and body["progress_pct"] == 0


def test_only_approved_cases_are_in_the_run_and_later_approvals_stay_out(client, lead, tester, cycle_id):
    ids = prepared(client, lead, tester, cycle_id, limit=2)
    run = client.post(f"/api/cycles/{cycle_id}/runs", headers=lead, json={"name": "R1"}).json()
    assert {r["tc_id"] for r in run["results"]} == set(ids)
    more = client.get(f"/api/cycles/{cycle_id}/test-cases", headers=lead, params={"test_type": "api", "status": "pending"}).json()
    client.patch(f"/api/cycles/{cycle_id}/test-cases", headers=tester, json={"tc_ids": [more[0]["id"]], "status": "approved"})
    again = client.get(f"/api/cycles/{cycle_id}/runs/{run['id']}", headers=lead).json()
    assert again["total"] == 2


def test_recording_results_and_report_maths(client, lead, tester, viewer, cycle_id):
    ids = prepared(client, lead, tester, cycle_id, limit=4)
    run = client.post(f"/api/cycles/{cycle_id}/runs", headers=lead, json={"name": "R1"}).json()
    rid = run["id"]
    assert result(client, viewer, cycle_id, rid, ids[0], "passed").status_code == 403
    assert result(client, tester, cycle_id, rid, ids[0], "passed").status_code == 200
    assert result(client, tester, cycle_id, rid, ids[1], "failed").status_code == 422  # needs notes
    assert result(client, tester, cycle_id, rid, ids[1], "failed", notes="Got 500", defect_ref="BUG-12").status_code == 200
    assert result(client, tester, cycle_id, rid, ids[2], "blocked", notes="env down").status_code == 200
    assert result(client, tester, cycle_id, rid, ids[3], "skipped").status_code == 200
    report = client.get(f"/api/cycles/{cycle_id}/runs/{rid}", headers=viewer).json()
    assert (report["passed"], report["failed"], report["blocked"], report["skipped"], report["not_run"]) == (1, 1, 1, 1, 0)
    assert report["progress_pct"] == 100
    assert report["pass_rate"] == 33  # 1 passed of 3 counted; the skipped case is left out
    assert [f["tc_id"] for f in report["failures"]][0] == ids[1]  # failed sorts before blocked
    assert report["failures"][0]["defect_ref"] == "BUG-12"
    assert report["by_type"]["api"]["passed"] == 1
    # changing a result back to not_run clears who and when
    result(client, tester, cycle_id, rid, ids[0], "not_run")
    row = next(r for r in client.get(f"/api/cycles/{cycle_id}/runs/{rid}", headers=lead).json()["results"] if r["tc_id"] == ids[0])
    assert row["executed_by"] is None and row["executed_at"] is None


def test_closed_run_is_read_only(client, lead, tester, cycle_id):
    ids = prepared(client, lead, tester, cycle_id, limit=1)
    rid = client.post(f"/api/cycles/{cycle_id}/runs", headers=lead, json={"name": "R1"}).json()["id"]
    assert client.post(f"/api/cycles/{cycle_id}/runs/{rid}/close", headers=tester).status_code == 403
    assert client.post(f"/api/cycles/{cycle_id}/runs/{rid}/close", headers=lead).json()["status"] == "closed"
    assert result(client, tester, cycle_id, rid, ids[0], "passed").status_code == 409


def test_unknown_run_or_case(client, lead, tester, cycle_id):
    ids = prepared(client, lead, tester, cycle_id, limit=1)
    rid = client.post(f"/api/cycles/{cycle_id}/runs", headers=lead, json={"name": "R1"}).json()["id"]
    assert client.get(f"/api/cycles/{cycle_id}/runs/999999", headers=lead).status_code == 404
    assert result(client, tester, cycle_id, rid, 999999, "passed").status_code == 404
    assert ids


def test_readiness_follows_the_latest_run(client, lead, tester, cycle_id):
    ids = prepared(client, lead, tester, cycle_id, limit=3)
    before = client.get(f"/api/cycles/{cycle_id}/readiness", headers=lead).json()["summary"]
    assert before["not_run"] >= 1 and before["passing"] == 0 and before["failing"] == 0

    run1 = client.post(f"/api/cycles/{cycle_id}/runs", headers=lead, json={"name": "R1"}).json()["id"]
    for tc_id in ids:
        result(client, tester, cycle_id, run1, tc_id, "failed", notes="broken")
    mid = client.get(f"/api/cycles/{cycle_id}/readiness", headers=lead).json()
    assert mid["summary"]["failing"] >= 1 and mid["summary"]["ready_pct"] == 0
    assert any(r["state"] == "failing" and r["failed"] >= 1 for r in mid["rows"])

    run2 = client.post(f"/api/cycles/{cycle_id}/runs", headers=lead, json={"name": "R2"}).json()["id"]
    for tc_id in ids:
        result(client, tester, cycle_id, run2, tc_id, "passed")
    after = client.get(f"/api/cycles/{cycle_id}/readiness", headers=lead).json()["summary"]
    assert after["failing"] == 0 and after["passing"] >= 1 and after["ready_pct"] > 0


def test_traceability_and_summary_carry_the_result(client, lead, tester, cycle_id):
    ids = prepared(client, lead, tester, cycle_id, limit=2)
    rid = client.post(f"/api/cycles/{cycle_id}/runs", headers=lead, json={"name": "R1"}).json()["id"]
    result(client, tester, cycle_id, rid, ids[0], "failed", notes="x")
    matrix = client.get(f"/api/cycles/{cycle_id}/traceability", headers=lead).json()
    assert all("result" in row for row in matrix["rows"])
    assert any(row["result"] == "failing" for row in matrix["rows"])
    summary = client.get(f"/api/projects", headers=lead).json()
    assert summary  # projects still load alongside the new tables


def test_runs_list_newest_first(client, lead, tester, cycle_id):
    prepared(client, lead, tester, cycle_id, limit=1)
    a = client.post(f"/api/cycles/{cycle_id}/runs", headers=lead, json={"name": "first"}).json()["id"]
    b = client.post(f"/api/cycles/{cycle_id}/runs", headers=lead, json={"name": "second"}).json()["id"]
    listed = client.get(f"/api/cycles/{cycle_id}/runs", headers=lead).json()
    assert [r["id"] for r in listed] == [b, a]
