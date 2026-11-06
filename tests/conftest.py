import os
import tempfile
from pathlib import Path

# Configure before the app is imported: isolated DB, no artificial streaming delay.
_tmp = tempfile.mkdtemp(prefix="trace-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_tmp, 'test.db').as_posix()}"
os.environ["GENERATION_DELAY_MS"] = "0"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["SEED_DEMO"] = "1"
os.environ["LLM_PROVIDER"] = "rules"

import time  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


def login(client, email):
    res = client.post("/api/auth/login", json={"email": email, "password": "demo123"})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


@pytest.fixture(scope="session")
def lead(client):
    return login(client, "lead@demo.local")


@pytest.fixture(scope="session")
def tester(client):
    return login(client, "tester@demo.local")


@pytest.fixture(scope="session")
def viewer(client):
    return login(client, "viewer@demo.local")


@pytest.fixture()
def cycle_id(client, lead):
    """A fresh project + cycle with the demo requirements copied in, so tests stay independent."""
    seed_cycle = client.get("/api/projects", headers=lead).json()[0]["cycles"][0]["id"]
    seed_reqs = client.get(f"/api/cycles/{seed_cycle}/requirements", headers=lead).json()
    project = client.post("/api/projects", headers=lead, json={"name": f"Test {time.time_ns()}"}).json()
    cid = project["cycles"][0]["id"]
    for r in seed_reqs:
        res = client.post(
            f"/api/cycles/{cid}/requirements",
            headers=lead,
            json={k: r[k] for k in ("title", "description", "req_type", "acceptance_criteria")},
        )
        assert res.status_code == 201, res.text
    return cid


def wait_for_run(client, headers, cid, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/cycles/{cid}/generate/status", headers=headers).json()
        if not data["active"] and data["events"]:
            return data
        time.sleep(0.05)
    raise AssertionError("generation did not finish in time")


def generate(client, headers, cid, **body):
    res = client.post(f"/api/cycles/{cid}/generate", headers=headers, json=body)
    assert res.status_code == 200, res.text
    return wait_for_run(client, headers, cid)
