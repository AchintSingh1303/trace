from app.services.quality import Subject, check_cycle, check_requirement, grade_for, score_findings


def codes(req):
    return {f.code for f in check_requirement(req)}


GOOD = Subject(
    "RQ-1",
    "User login",
    "Registered users sign in with email and password to reach their dashboard.",
    "Functional",
    ["A valid user reaches the dashboard", "A wrong password shows an error message", "The account locks after 5 failed attempts"],
)


def test_good_requirement_is_clean():
    assert check_requirement(GOOD) == []
    assert score_findings([]) == 100 and grade_for(100) == "A"


def test_missing_criteria_is_an_error():
    req = Subject("RQ-2", "Export", "Users can export their data to a file.", "Functional", [])
    assert "NO_CRITERIA" in codes(req)


def test_placeholder_and_vague_terms():
    req = Subject("RQ-3", "Search", "The search should be fast and easy, TBD.", "Functional", ["Results appear quickly", "Shows matches etc"])
    found = codes(req)
    assert {"PLACEHOLDER", "VAGUE_TERM"} <= found
    vague = [f for f in check_requirement(req) if f.code == "VAGUE_TERM"]
    assert {f.field for f in vague} >= {"description", "acceptance_criteria[0]"}


def test_vague_word_inside_another_word_is_not_flagged():
    req = Subject("RQ-4", "Fasten", "Users fasten the strap and get a confirmation message.", "Functional", ["The strap fastens and the light turns green"])
    assert "VAGUE_TERM" not in codes(req)


def test_performance_needs_a_number():
    req = Subject("RQ-5", "Checkout speed", "Checkout must respond promptly under load.", "Performance", ["Checkout responds under load"])
    assert "UNMEASURED_PERFORMANCE" in codes(req)
    req.acceptance_criteria = ["Checkout responds within 2 seconds at 200 users"]
    assert "UNMEASURED_PERFORMANCE" not in codes(req)


def test_api_requirement_needs_method_and_path():
    req = Subject("RQ-6", "Create orders", "Clients create orders through the service.", "API", ["A valid request returns 201"])
    assert "API_NO_ENDPOINT" in codes(req)
    req.title = "POST /orders creates an order"
    assert "API_NO_ENDPOINT" not in codes(req)


def test_short_and_compound_criteria():
    req = Subject("RQ-7", "Profile", "Users edit the details shown on their profile page.", "Functional", ["Works", "Saves the name and the photo and the bio, or shows an error"])
    found = codes(req)
    assert "SHORT_CRITERION" in found and "COMPOUND_CRITERION" in found


def test_score_and_grade_boundaries():
    assert grade_for(85) == "A" and grade_for(84) == "B" and grade_for(70) == "B"
    assert grade_for(69) == "C" and grade_for(50) == "C" and grade_for(49) == "D"
    req = Subject("RQ-8", "x", "", "Functional", [])
    findings = check_requirement(req)
    assert score_findings(findings) == 100 - 25 - 3  # no criteria + thin description


def test_near_duplicates_are_flagged_in_a_cycle():
    a = Subject("RQ-1", "Reset password by email", "Users reset a forgotten password through an emailed link.", "Functional", ["The email contains a reset link"])
    b = Subject("RQ-2", "Reset password by email", "Users reset a forgotten password through an emailed link.", "Functional", ["The link expires after one hour"])
    result = check_cycle([a, b])
    assert all(any(f["code"] == "NEAR_DUPLICATE" for f in r["findings"]) for r in result["requirements"])
    assert result["summary"]["count"] == 2


def test_cycle_endpoint(client, lead, viewer, cycle_id):
    res = client.get(f"/api/cycles/{cycle_id}/quality", headers=viewer)
    assert res.status_code == 200
    data = res.json()
    assert data["summary"]["count"] == 11
    assert set(data["summary"]["grades"]) == {"A", "B", "C", "D"}
    assert all({"code", "score", "grade", "findings"} <= set(r) for r in data["requirements"])
    assert client.get("/api/cycles/999999/quality", headers=lead).status_code == 404
