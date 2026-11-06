from app.services import agents as A
from app.services.llm import parse_tool_input


def req(code="RQ-001", title="Login", req_type="Functional", ac=None, description="", id=1):
    return A.ReqView(id, code, title, description, req_type, ac or [], 1)


def test_boundary_detects_lower_bound_and_builds_three_values():
    r = req(ac=["The password must be at least 8 characters"])
    case = A.boundary_case(r, r.acceptance_criteria[0], "functional")
    assert case is not None
    assert [d["value"] for d in case.test_data] == ["7", "8", "9"]
    assert "rejected" in case.steps[1]["expected"]  # 7 is below the minimum
    assert "accepted" in case.steps[3]["expected"]  # 9 is fine


def test_boundary_detects_upper_bound():
    r = req(ac=["Quantity is limited to a maximum of 10 per item"])
    case = A.boundary_case(r, r.acceptance_criteria[0], "functional")
    assert [d["value"] for d in case.test_data] == ["9", "10", "11"]
    assert "rejected" in case.steps[3]["expected"]  # 11 is over the limit


def test_functional_agent_splits_negative_and_happy_paths():
    r = req(ac=["User lands on the dashboard", "An incorrect password shows an error"])
    cases = A.run_agent("functional", [r], [r])
    subtypes = {c.subtype for c in cases}
    assert {"smoke", "happy_path", "negative"} <= subtypes
    assert all(c.req_codes == ["RQ-001"] for c in cases)
    assert all(c.steps for c in cases)


def test_api_agent_parses_endpoint_and_adds_not_found_for_id_paths():
    r = req(title="GET /products/{id} returns a product", req_type="API", ac=["Returns the product"])
    cases = A.run_agent("api", [r], [r])
    titles = " ".join(c.title for c in cases)
    assert "GET /products/{id}" in titles
    assert any(c.subtype == "not_found" for c in cases)


def test_performance_agent_reads_users_and_p95_from_text():
    r = req(title="Search latency", req_type="Performance", description="p95 under 800 ms at 200 concurrent users")
    cases = A.run_agent("performance", [r], [r])
    load = next(c for c in cases if c.subtype == "load")
    data = {d["field_name"]: d["value"] for d in load.test_data}
    assert data["peak_value"] == "200"
    assert data["target_p95_ms"] == "800"
    stress = next(c for c in cases if c.subtype == "stress")
    assert {d["field_name"]: d["value"] for d in stress.test_data}["peak_value"] == "400"


def test_integration_agent_links_journey_to_related_requirements():
    journey = req("RQ-010", "Guest to paid order", "Journey", ["Add the product to the cart"], id=10)
    cart = req("RQ-002", "Add to cart", "Functional", ["Adding an item increases the cart count"], id=2)
    cases = A.run_agent("integration", [journey], [journey, cart])
    flow = next(c for c in cases if c.subtype == "journey")
    assert flow.req_codes[0] == "RQ-010"


def test_dedupe_cap_and_split():
    r = req(ac=["Does a thing", "Does a thing"])
    titles = [c.title for c in A.run_agent("functional", [r], [r])]
    assert len(titles) == len(set(t.lower() for t in titles))
    assert A.split_cap(10, ["a", "b", "c"]) == {"a": 4, "b": 3, "c": 3}
    assert A.split_cap(None, ["a"]) == {"a": None}


def test_llm_output_is_validated_before_use():
    payload = {
        "test_cases": [
            {"title": "[Happy Path] ok", "priority": "urgent", "req_codes": ["RQ-001", "RQ-999"], "steps": [{"action": "Do", "expected": "Done"}]},
            {"title": "no steps", "priority": "high", "req_codes": ["RQ-001"], "steps": []},
            {"title": "unknown requirement only", "priority": "high", "req_codes": ["RQ-404"], "steps": [{"action": "x", "expected": "y"}]},
        ]
    }
    cases = parse_tool_input(payload, "functional", {"RQ-001"})
    assert len(cases) == 1
    assert cases[0].priority == "medium"  # unknown priority normalised
    assert cases[0].req_codes == ["RQ-001"]  # unknown code dropped
    assert cases[0].tag == "Happy Path"
