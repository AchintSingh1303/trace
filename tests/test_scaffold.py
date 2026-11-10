import ast

from conftest import generate
from test_runs import approve_some

from app.models import TestCase
from app.services.scaffold import _func_name, _line


def test_needs_lead_and_approved_api_cases(client, lead, tester, cycle_id):
    url = f"/api/cycles/{cycle_id}/export/pytest"
    assert client.get(url, headers=lead).status_code == 409
    generate(client, lead, cycle_id, test_types=["api"])
    assert client.get(url, headers=lead).status_code == 409  # generated but nothing approved
    approve_some(client, lead, tester, cycle_id, limit=5)
    assert client.get(url, headers=tester).status_code == 403
    assert client.get(f"/api/cycles/999999/export/pytest", headers=lead).status_code == 404


def test_output_is_valid_python_with_markers_and_steps(client, lead, tester, cycle_id):
    generate(client, lead, cycle_id, test_types=["api"])
    approve_some(client, lead, tester, cycle_id, limit=6)
    res = client.get(f"/api/cycles/{cycle_id}/export/pytest", headers=lead)
    assert res.status_code == 200
    assert "attachment" in res.headers["content-disposition"]
    source = res.text
    tree = ast.parse(source)
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    assert funcs and len(funcs) == int(res.headers["x-trace-written"])
    assert "pytest.mark.requirement(" in source
    assert "# Step 1:" in source and "#   Expected:" in source
    assert "requests.request(" in source
    assert "TODO" in source


def test_case_without_endpoint_is_listed_not_guessed(client, lead, tester, cycle_id):
    from app.db import SessionLocal

    generate(client, lead, cycle_id, test_types=["api"])
    ids = approve_some(client, lead, tester, cycle_id, limit=3)
    with SessionLocal() as db:
        tc = db.get(TestCase, ids[0])
        code = tc.tc_code
        for item in list(tc.test_data):
            if item.field_name == "endpoint":
                db.delete(item)
        db.commit()
    res = client.get(f"/api/cycles/{cycle_id}/export/pytest", headers=lead)
    assert res.headers["x-trace-skipped"] == "1"
    assert f"#   {code}" in res.text
    assert _func_name(code) not in res.text.split("# Not written")[0]
    ast.parse(res.text)


def test_awkward_text_cannot_break_the_file():
    assert "\n" not in _line("line one\nline two\r\n  three")
    assert _func_name("TC-API-001") == "test_tc_api_001"
    assert _func_name("weird code/1!") == "test_weird_code_1"
