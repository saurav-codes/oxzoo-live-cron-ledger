import os
import subprocess
import sys

import pytest

import zoo

PANEL = "http://localhost:5173"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ZOO_PANEL_ORIGIN", PANEL)
    monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", "postgres://u@127.0.0.1:1/none"))
    import app
    return app.app.test_client()


def test_health_cors(client):
    r = client.get("/_zoo/health", headers={"Origin": PANEL})
    assert r.status_code == 200 and r.json["name"] == "cron-ledger"
    assert r.headers["Access-Control-Allow-Origin"] == PANEL and r.headers["Vary"] == "Origin"
    assert "Access-Control-Allow-Origin" not in client.get("/_zoo/health", headers={"Origin": "https://evil.example"}).headers


def test_preflight(client):
    r = client.options("/_zoo/probe", headers={"Origin": PANEL, "Access-Control-Request-Method": "GET"})
    assert r.status_code == 204
    assert r.headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS" and r.headers["Access-Control-Max-Age"] == "600"
    r = client.options("/_zoo/probe", headers={"Origin": "https://evil.example"})
    assert r.status_code == 204 and "Access-Control-Allow-Origin" not in r.headers


def test_probe_fails_honestly_without_database(client):
    body = client.get("/_zoo/probe").json
    assert body["ok"] is False and {c["id"] for c in body["checks"]} == {"postgres", "cron:tick", "cron:rollup", "idempotency"}
    assert all(c["env"] == ["DATABASE_URL"] for c in body["checks"])


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="set TEST_DATABASE_URL")
def test_probe_and_page_with_database(client):
    subprocess.run([sys.executable, "migrate.py"], check=True, env=os.environ | {"DATABASE_URL": os.environ["TEST_DATABASE_URL"]})
    for job in ("tick", "rollup", "tick"):
        subprocess.run([sys.executable, "jobs.py", job], check=True, env=os.environ | {"DATABASE_URL": os.environ["TEST_DATABASE_URL"]})
    body = client.get("/_zoo/probe").json
    assert body["ok"], body
    page = client.get("/")
    assert page.status_code == 200 and b"rollup" in page.data
    assert {j["job"] for j in client.get("/api/runs").json["jobs"]} == {"tick", "rollup", "prune", "report"}
