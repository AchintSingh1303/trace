"""Claude and ChatGPT generators, tested against a mocked HTTP layer (no network, no real keys)."""

import json
import re

import httpx
import pytest
from conftest import generate, wait_for_run

from app.services import agents as A
from app.services import llm

KEY = "sk-test-SECRET-1234567890"


def req(code="RQ-001"):
    return A.ReqView(1, code, "Login", "", "Functional", ["User can sign in"], 1)


def tool_payload(code):
    return {
        "test_cases": [
            {
                "title": "[Happy Path] Verify that a model-written case is stored",
                "subtype": "happy_path",
                "priority": "high",
                "req_codes": [code],
                "steps": [{"action": "Do the thing", "expected": "It works"}],
            }
        ]
    }


def install_mock(monkeypatch, handler):
    monkeypatch.setattr(llm, "http_factory", lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def smart_handler(seen):
    """Behaves like either provider, based on the URL, and records what was sent."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((str(request.url), dict(request.headers), body))
        if "anthropic" in str(request.url):
            text = body["messages"][0]["content"]
            if "tools" not in body:
                return httpx.Response(200, json={"content": [{"type": "text", "text": "OK"}]})
            code = re.search(r"RQ-\d+", text).group(0)
            return httpx.Response(200, json={"stop_reason": "tool_use", "content": [{"type": "tool_use", "name": llm.TOOL_NAME, "input": tool_payload(code)}]})
        text = body["messages"][1]["content"]
        if "tools" not in body:
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
        code = re.search(r"RQ-\d+", text).group(0)
        call = {"function": {"name": llm.TOOL_NAME, "arguments": json.dumps(tool_payload(code))}}
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [call]}}]})

    return handler


# ------------------------------------------------------------------ provider layer


def test_resolve_config_requires_a_key_and_applies_defaults(monkeypatch):
    assert llm.resolve_config("rules", None, None) is None
    with pytest.raises(ValueError, match="API key"):
        llm.resolve_config("openai", "", None)
    cfg = llm.resolve_config("anthropic", f"  {KEY} ", None)
    assert cfg.api_key == KEY and cfg.model == llm.PROVIDERS["anthropic"]["default_model"]
    assert KEY not in repr(cfg)  # a key must never show up in logs or tracebacks


def test_claude_request_shape_and_parsing(monkeypatch):
    seen = []
    install_mock(monkeypatch, smart_handler(seen))
    cfg = llm.LLMConfig("anthropic", KEY, "model-x")
    cases = llm.generate_with_llm("functional", [req()], A.ProjectView("P"), cfg)

    url, headers, body = seen[0]
    assert url == llm.PROVIDERS["anthropic"]["url"]
    assert headers["x-api-key"] == KEY and headers["anthropic-version"]
    assert body["model"] == "model-x" and body["tool_choice"] == {"type": "tool", "name": llm.TOOL_NAME}
    assert [c.title for c in cases] == ["[Happy Path] Verify that a model-written case is stored"]


def test_chatgpt_request_shape_and_parsing(monkeypatch):
    seen = []
    install_mock(monkeypatch, smart_handler(seen))
    cfg = llm.LLMConfig("openai", KEY, "model-y")
    cases = llm.generate_with_llm("api", [req()], A.ProjectView("P"), cfg)

    url, headers, body = seen[0]
    assert url == llm.PROVIDERS["openai"]["url"]
    assert headers["authorization"] == f"Bearer {KEY}"
    assert body["model"] == "model-y" and body["tool_choice"]["function"]["name"] == llm.TOOL_NAME
    assert cases and cases[0].test_type == "api"


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_provider_errors_are_friendly_and_never_contain_the_key(monkeypatch, provider):
    def handler(request):
        return httpx.Response(400, json={"error": {"message": f"bad request near {KEY} sorry"}})

    install_mock(monkeypatch, handler)
    with pytest.raises(llm.LLMError) as err:
        llm.check_connection(llm.LLMConfig(provider, KEY, "m"))
    assert KEY not in str(err.value) and "[your key]" in str(err.value)


def test_rejected_key_gets_a_clear_message(monkeypatch):
    install_mock(monkeypatch, lambda r: httpx.Response(401, json={"error": {"message": f"Incorrect API key {KEY}"}}))
    with pytest.raises(llm.LLMError, match="rejected the API key") as err:
        llm.check_connection(llm.LLMConfig("openai", KEY, "m"))
    assert KEY not in str(err.value)


def test_truncated_and_malformed_answers_are_reported(monkeypatch):
    install_mock(monkeypatch, lambda r: httpx.Response(200, json={"stop_reason": "max_tokens", "content": []}))
    with pytest.raises(llm.LLMError, match="cut off"):
        llm.generate_with_llm("functional", [req()], A.ProjectView("P"), llm.LLMConfig("anthropic", KEY, "m"))
    install_mock(monkeypatch, lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "no tool call"}}]}))
    with pytest.raises(llm.LLMError, match="expected format"):
        llm.generate_with_llm("functional", [req()], A.ProjectView("P"), llm.LLMConfig("openai", KEY, "m"))


# ------------------------------------------------------------------ through the API


def everything(client, headers, cid):
    """Every piece of text the app exposes about a run, to prove a key is not in any of it."""
    pieces = [
        json.dumps(client.get(f"/api/cycles/{cid}/generate/status", headers=headers).json()),
        json.dumps(client.get(f"/api/cycles/{cid}", headers=headers).json()),
        json.dumps(client.get(f"/api/cycles/{cid}/audit", headers=headers).json()),
        json.dumps(client.get(f"/api/cycles/{cid}/test-cases", headers=headers).json()),
    ]
    return "\n".join(pieces)


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_generate_with_a_chosen_provider_stores_cases_and_never_the_key(client, lead, cycle_id, monkeypatch, provider):
    seen = []
    install_mock(monkeypatch, smart_handler(seen))
    res = client.post(
        f"/api/cycles/{cycle_id}/generate",
        headers=lead,
        json={"selected_test_types": ["functional", "api"], "provider": provider, "api_key": KEY, "model": "custom-model"},
    )
    assert res.status_code == 200 and res.json()["provider"] == provider and res.json()["model"] == "custom-model"
    done = wait_for_run(client, lead, cycle_id)

    assert done["events"][-1]["type"] == "generation_complete"
    assert done["events"][0]["data"]["model"] == "custom-model"
    stored = client.get(f"/api/cycles/{cycle_id}/test-cases", headers=lead).json()
    assert stored and all("model-written case" in c["title"] for c in stored)
    assert {call[2]["model"] for call in seen} == {"custom-model"}
    assert KEY not in everything(client, lead, cycle_id)
    assert client.get(f"/api/cycles/{cycle_id}", headers=lead).json()["summary"]["last_run"]["provider"] == provider


def test_a_failing_provider_falls_back_to_rules_and_says_so(client, lead, cycle_id, monkeypatch):
    install_mock(monkeypatch, lambda r: httpx.Response(401, json={"error": {"message": f"nope {KEY}"}}))
    client.post(
        f"/api/cycles/{cycle_id}/generate",
        headers=lead,
        json={"selected_test_types": ["security"], "provider": "openai", "api_key": KEY},
    )
    done = wait_for_run(client, lead, cycle_id)
    kinds = [e["type"] for e in done["events"]]
    assert "agent_warning" in kinds and kinds[-1] == "generation_complete"
    complete = next(e for e in done["events"] if e["type"] == "agent_complete")
    assert complete["data"]["generator"] == "rules (fallback)" and complete["data"]["total"] > 0
    assert KEY not in everything(client, lead, cycle_id)


def test_generate_validates_provider_and_key(client, lead, viewer, cycle_id):
    url = f"/api/cycles/{cycle_id}/generate"
    assert client.post(url, headers=lead, json={"provider": "anthropic"}).status_code == 422  # no key
    assert client.post(url, headers=lead, json={"provider": "gemini", "api_key": KEY}).status_code == 422
    assert client.post(url, headers=viewer, json={"provider": "openai", "api_key": KEY}).status_code == 403


def test_provider_list_and_key_check_endpoint(client, lead, viewer, monkeypatch):
    listing = client.get("/api/llm/providers", headers=viewer).json()
    assert [p["id"] for p in listing["providers"]] == ["rules", "anthropic", "openai"]
    assert "api_key" not in json.dumps(listing)

    install_mock(monkeypatch, smart_handler([]))
    ok = client.post("/api/llm/check", headers=lead, json={"provider": "anthropic", "api_key": KEY})
    assert ok.status_code == 200 and ok.json()["ok"] is True
    assert client.post("/api/llm/check", headers=viewer, json={"provider": "anthropic", "api_key": KEY}).status_code == 403

    install_mock(monkeypatch, lambda r: httpx.Response(401, json={}))
    bad = client.post("/api/llm/check", headers=lead, json={"provider": "openai", "api_key": KEY})
    assert bad.status_code == 400 and KEY not in bad.text
