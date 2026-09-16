"""The fleet-updates proxy (roadmap AK), with spark-fleet-updates stubbed.

What is worth testing is the contract, not the plumbing: which routes exist
and which do not, that the real scheme travels on, that the fleet service's
own refusals reach the caller with their wording intact, and that "off",
"unreachable" and "empty" are three different answers.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from spark_dash_backend.app import create_app
from spark_dash_backend.config import Settings

FLEET = {"nodes": [{"name": "sparky", "host": "192.168.50.61", "reachable": True}], "clusters": []}


class Upstream:
    """A fake fleet service: records every request, answers from a script."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.body: object = FLEET
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            raise httpx.ConnectError("refused")
        return httpx.Response(self.status, json=self.body)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


def make_client(tmp_path, monkeypatch, **overrides):
    async def fake_healthy(self):
        return True

    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.healthy", fake_healthy)
    app = create_app(
        Settings(
            spark_nodes="gx10-1=192.168.50.61",
            prometheus_targets_dir=tmp_path,
            static_dir=tmp_path / "nostatic",
            **overrides,
        )
    )
    return app


@pytest.fixture
def off(tmp_path, monkeypatch):
    app = make_client(tmp_path, monkeypatch)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def upstream():
    return Upstream()


@pytest.fixture
def on(tmp_path, monkeypatch, upstream):
    app = make_client(
        tmp_path,
        monkeypatch,
        fleet_updates_url="http://spark-fleet-updates:8080",
        fleet_updates_public_url="https://fleet.example.test",
    )
    app.state.fleet_updates._transport = httpx.MockTransport(upstream.handler)
    with TestClient(app) as c:
        yield c


# --- off ------------------------------------------------------------------


def test_not_configured_is_said_not_implied(off):
    body = off.get("/api/fleet").json()
    assert body == {"configured": False, "available": False, "public_url": None, "fleet": None}


def test_not_configured_refuses_every_action(off):
    assert off.post("/api/fleet/check").status_code == 404
    assert off.post("/api/fleet/nodes/sparky/update", json={}).status_code == 404
    assert off.post("/api/fleet/runs/r1/stop").status_code == 404
    assert off.get("/api/fleet/runs/r1/sparky/log").status_code == 404


# --- on -------------------------------------------------------------------


def test_fleet_passes_through_with_the_public_url(on, upstream):
    body = on.get("/api/fleet").json()
    assert body["configured"] is True
    assert body["available"] is True
    assert body["public_url"] == "https://fleet.example.test"
    assert body["fleet"] == FLEET
    assert upstream.last.url.path == "/api/fleet"


def test_unreachable_is_not_an_empty_fleet(on, upstream):
    upstream.fail = True
    body = on.get("/api/fleet").json()
    assert body["configured"] is True
    assert body["available"] is False
    assert body["fleet"] is None


def test_actions_forward_to_the_fleet_service_paths(on, upstream):
    upstream.body = {"ok": True}
    on.post("/api/fleet/check")
    assert (upstream.last.method, upstream.last.url.path) == ("POST", "/api/check")
    on.post("/api/fleet/nodes/sparky/check")
    assert upstream.last.url.path == "/api/nodes/sparky/check"
    on.post("/api/fleet/nodes/sparky/rehearse")
    assert upstream.last.url.path == "/api/nodes/sparky/rehearse"
    on.post("/api/fleet/runs/r1/stop")
    assert upstream.last.url.path == "/api/runs/r1/stop"
    on.post("/api/fleet/runs/r1/verify")
    assert upstream.last.url.path == "/api/runs/r1/verify"
    on.get("/api/fleet/runs/r1/sparky/log")
    assert (upstream.last.method, upstream.last.url.path) == ("GET", "/api/runs/r1/sparky/log")


def test_the_password_travels_and_nothing_else_does(on, upstream):
    upstream.body = {"id": "r1"}
    on.post("/api/fleet/nodes/sparky/update", json={"password": "hunter2", "extra": "dropped"})
    assert json.loads(upstream.last.content) == {"password": "hunter2"}
    # No password: an empty body, so the fleet service takes its sudo -n path.
    on.post("/api/fleet/nodes/sparky/update", json={})
    assert json.loads(upstream.last.content) == {}


def test_enrol_hands_over_the_id_and_host_and_nothing_else(on, upstream):
    upstream.body = {"name": "sparky", "host": "192.168.50.61"}
    resp = on.post(
        "/api/fleet/nodes", json={"name": "sparky", "host": "192.168.50.61", "user": "x"}
    )
    assert resp.status_code == 200
    assert (upstream.last.method, upstream.last.url.path) == ("POST", "/api/nodes")
    assert json.loads(upstream.last.content) == {"name": "sparky", "host": "192.168.50.61"}
    assert on.post("/api/fleet/nodes", json={"name": "", "host": "h"}).status_code == 422


def test_remove_is_routed_and_rename_is_not(on, upstream):
    upstream.body = {"ok": True}
    on.post("/api/fleet/nodes/sparky/remove")
    assert upstream.last.url.path == "/api/nodes/sparky/remove"
    assert on.post("/api/fleet/nodes/sparky/rename", json={"name": "x"}).status_code == 422
    assert on.post("/api/fleet/runs/r1/delete").status_code == 422
    assert len(upstream.requests) == 1


def test_the_real_scheme_is_forwarded_never_assumed(on, upstream):
    upstream.body = {"ok": True}
    # Plain HTTP in, plain HTTP on: the fleet service's password rule sees
    # the truth and refuses if it must.
    on.post("/api/fleet/nodes/sparky/update", json={})
    assert upstream.last.headers["x-forwarded-proto"] == "http"
    # Through the tunnel cloudflared says https, and that is passed on.
    on.post("/api/fleet/nodes/sparky/update", json={}, headers={"X-Forwarded-Proto": "https"})
    assert upstream.last.headers["x-forwarded-proto"] == "https"


def test_the_fleet_services_refusal_reaches_the_caller_verbatim(on, upstream):
    upstream.status = 400
    upstream.body = {"error": "a password is only accepted over HTTPS"}
    resp = on.post("/api/fleet/nodes/sparky/update", json={"password": "x"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "a password is only accepted over HTTPS"


def test_a_dead_fleet_service_is_a_502_on_an_action(on, upstream):
    upstream.fail = True
    resp = on.post("/api/fleet/check")
    assert resp.status_code == 502
    assert resp.json()["detail"].startswith("spark-fleet-updates:")


# --- health ---------------------------------------------------------------


def test_health_names_the_fleet_service_state(off, on, upstream, monkeypatch):
    async def no_poll(self):
        return None

    monkeypatch.setattr("spark_dash_backend.poller.LivePoller.poll_once", no_poll)
    assert off.get("/health").json()["fleet_updates"] == "not configured"
    assert on.get("/health").json()["fleet_updates"] == "ok"
    upstream.fail = True
    body = on.get("/health").json()
    assert body["fleet_updates"] == "unreachable"
    # Not a problem: the dashboard sees the cluster fine without it.
    assert not any("fleet" in p for p in body["problems"])
