"""The `/api/fleet*` routes when this process IS the fleet updater (AL3f).

The twin of `test_fleet_updates.py`, which asserts the same routes against the
proxy. That file can check the shape of the request going out; there is no
request here, so these check what a caller sees and what actually happened to
the state on disk.

Worth having both while both implementations exist: `app.py` has one set of
routes and picks an implementation once, so a break in the seam shows up as a
route that works over the wire and not in-process, or the reverse. When AL3g
deletes the proxy, this file is what is left.

`ssh.run` is faked. Nothing here reaches a Spark.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from spark_dash_backend.app import create_app
from spark_dash_backend.config import Settings
from spark_dash_backend.fleet import ssh


@pytest.fixture
def calls(monkeypatch):
    """Every ssh command the app would have run, and a refusal in reply.

    A refusal rather than a success on purpose: it makes an update hold at its
    first step, which keeps these tests to the routes rather than dragging the
    whole state machine through each one. The machine itself is covered in
    test_fleet_embedded.py.
    """
    seen: list[tuple[str, str]] = []

    def fake_run(host, command, *, user=None, stdin=None, timeout=60):
        seen.append((host, command))
        return ssh.Result(255, "", "ssh: connect to host port 22: No route to host")

    monkeypatch.setattr(ssh, "run", fake_run)
    return seen


# The proxy in front, as TRUSTED_PROXIES names it (CV), and who is calling.
# By default the caller IS that proxy; a test that wants someone else
# parametrizes `peer`.
TRUSTED = "10.9.0.0/24"
PROXY = "10.9.0.5"


@pytest.fixture
def peer():
    return PROXY


@pytest.fixture
def client(tmp_path, monkeypatch, calls, peer):
    async def fake_healthy(self):
        return True

    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.healthy", fake_healthy)
    key = tmp_path / "id_ed25519"
    key.write_text("not a real key")
    (tmp_path / "targets").mkdir()   # the real one is a mount; here it just has to exist
    app = create_app(
        Settings(
            spark_nodes="sparky=192.168.50.61,sparketa=192.168.50.62",
            prometheus_targets_dir=tmp_path / "targets",
            static_dir=tmp_path / "nostatic",
            fleet_updates_url="",
            fleet_ssh_user="brian",
            fleet_ssh_key=key,
            fleet_state_dir=tmp_path / "state",
            trusted_proxies=TRUSTED,
        )
    )
    with TestClient(app, client=(peer, 50000)) as c:
        yield c
    ssh.configure()


def enrol(client: TestClient, name: str = "sparky") -> None:
    resp = client.post("/api/fleet/nodes", json={"name": name, "host": "192.168.50.61"})
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------- the routes


def test_the_envelope_carries_what_the_panel_reads(client):
    """One envelope, and every field in it is something the panel renders.

    `public_url` was here until AL3g: a link to the separate container's own
    page, for the few things the panel could not do. There is no other page
    now, and the panel does all of it."""
    body = client.get("/api/fleet").json()
    assert body["configured"] is True
    assert body["available"] is True
    assert body["embedded"] is True
    assert set(body) >= {"configured", "available", "fleet", "capability",
                         "enabled", "requirements"}
    assert "public_url" not in body
    assert body["fleet"]["nodes"] == []


def test_enrol_takes_the_id_the_dashboard_knows_and_refuses_one_it_does_not(client):
    """AL3d: the host in the body is ignored -- cluster.yml is the address --
    but a name the dashboard has never heard of cannot be enrolled at all."""
    enrol(client)
    nodes = client.get("/api/fleet").json()["fleet"]["nodes"]
    assert [n["name"] for n in nodes] == ["sparky"]

    refused = client.post("/api/fleet/nodes", json={"name": "ghost", "host": "10.0.0.1"})
    assert refused.status_code == 400
    assert "not a node this dashboard knows" in refused.json()["detail"]
    # and the validation on the model still holds
    assert client.post("/api/fleet/nodes", json={"name": "", "host": "h"}).status_code == 422


def test_check_reaches_the_node_and_remove_takes_it_off_the_list(client, calls):
    enrol(client)
    calls.clear()
    assert client.post("/api/fleet/nodes/sparky/check").json() == {"ok": True}
    for _ in range(50):
        if calls:
            break
        __import__("time").sleep(0.05)
    assert calls and calls[0][0] == "192.168.50.61", "the check never reached the node"

    assert client.post("/api/fleet/nodes/sparky/remove").json() == {"ok": True}
    assert client.get("/api/fleet").json()["fleet"]["nodes"] == []


def test_check_everything_sweeps_without_waiting_for_it(client, calls):
    """The button returns at once and the panel switches to its 3s cadence."""
    enrol(client)
    calls.clear()
    assert client.post("/api/fleet/check").json() == {"ok": True}
    for _ in range(50):
        if calls:
            break
        __import__("time").sleep(0.05)
    assert calls, "the sweep never started"


def test_update_and_rehearse_start_a_run_and_stop_ends_it(client):
    enrol(client)
    state = client.post("/api/fleet/nodes/sparky/rehearse").json()
    assert state["rehearsal"] is True
    assert [n["name"] for n in state["nodes"]] == ["sparky"]

    run_id = state["id"]
    assert client.post(f"/api/fleet/runs/{run_id}/stop").json() == {"ok": True}
    log = client.get(f"/api/fleet/runs/{run_id}/sparky/log").json()
    assert isinstance(log["lines"], list)


def test_unknown_things_are_404_and_a_busy_spark_is_409(client):
    """The fleet service's own statuses and wording, kept."""
    assert client.post("/api/fleet/nodes/nobody/check").status_code == 404
    assert client.post("/api/fleet/runs/no-such-run/stop").status_code == 404
    assert client.post("/api/fleet/runs/no-such-run/verify").status_code == 404

    enrol(client)
    first = client.post("/api/fleet/nodes/sparky/update").json()
    busy = client.post("/api/fleet/nodes/sparky/update")
    assert busy.status_code == 409
    assert "already being updated" in busy.json()["detail"]
    client.post(f"/api/fleet/runs/{first['id']}/stop")


def test_only_the_four_actions_exist_and_nothing_else_happens(client, calls):
    """A `Literal` on the action, so anything else is unreachable by URL rather
    than merely undocumented. Rename went with AL3d -- a dashboard node's id IS
    the fleet name."""
    enrol(client)
    calls.clear()
    assert client.post("/api/fleet/nodes/sparky/rename", json={"name": "x"}).status_code == 422
    assert client.post("/api/fleet/nodes/sparky/delete").status_code == 422
    assert client.post("/api/fleet/runs/r1/delete").status_code == 422
    assert calls == [], "a rejected action still touched a Spark"


# -------------------------------------------------------------- the password


def test_a_password_off_tls_is_refused_and_not_echoed(client):
    """The rule that does not soften because the network hop disappeared."""
    enrol(client)
    resp = client.post("/api/fleet/nodes/sparky/update", json={"password": "hunter2"})
    assert resp.status_code == 400
    assert "only accepted over HTTPS" in resp.json()["detail"]
    assert "hunter2" not in resp.text


def test_the_real_scheme_is_read_never_assumed(client):
    """The trusted proxy's header, else the scheme this server saw. An absent
    header is plain HTTP, not "probably fine"."""
    enrol(client)
    ok = client.post(
        "/api/fleet/nodes/sparky/update",
        json={"password": "hunter2"},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert ok.status_code == 200, ok.text
    client.post(f"/api/fleet/runs/{ok.json()['id']}/stop")


def test_a_malformed_password_field_is_not_echoed_back(client):
    """AL2. This is why the update routes read their body by hand: a pydantic
    model that fails validation renders the offending value into its 422, and
    the offending value here is the password. Anything that is not a non-empty
    string is no password at all, which is the sudo -n path."""
    enrol(client)
    for body in ({"password": 123}, {"password": {"secret": "hunter2"}}, {"password": None}):
        resp = client.post("/api/fleet/nodes/sparky/update", json=body)
        assert resp.status_code != 422, f"{body} reached pydantic: {resp.text}"
        assert "hunter2" not in resp.text
        if resp.status_code == 200:
            client.post(f"/api/fleet/runs/{resp.json()['id']}/stop")


def test_a_body_that_is_not_json_at_all_is_simply_no_password(client):
    """The panel sends JSON; curl and a mistyped fetch send other things. None
    of them should be a 500, and none should be a password."""
    enrol(client)
    resp = client.post(
        "/api/fleet/nodes/sparky/update",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    client.post(f"/api/fleet/runs/{resp.json()['id']}/stop")


def test_nothing_a_run_wrote_contains_the_password(client, tmp_path):
    """Belt and braces over the same check in test_fleet_embedded, this time
    through the route a browser actually posts to."""
    enrol(client)
    resp = client.post(
        "/api/fleet/nodes/sparky/update",
        json={"password": "hunter2"},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert resp.status_code == 200
    run_id = resp.json()["id"]
    client.post(f"/api/fleet/runs/{run_id}/stop")

    # Wait for the run thread to finish before reading what it left. Without
    # this the test is a race it usually wins: `_save` writes state.tmp and
    # renames it, so a glob can list a file that is gone a moment later. A
    # flaky guard on the password is worse than no guard -- it teaches you to
    # re-run the suite instead of reading the failure.
    client.app.state.fleet_updates.svc.runs[run_id].thread.join(timeout=15)

    written = [f for f in (tmp_path / "state" / "runs").rglob("*") if f.is_file()]
    assert written, "the run wrote nothing; this would pass vacuously"
    for f in written:
        assert "hunter2" not in f.read_text(errors="replace"), f"the password reached {f.name}"


# ------------------------------------------- who may say HTTPS (roadmap CV)
#
# X-Forwarded-Proto is believed only from TRUSTED_PROXIES. Before CV any
# client could send it, so a plain-HTTP request straight to the backend with
# the header forged was accepted with a password.

LAN = "192.168.50.20"


@pytest.mark.parametrize(
    ("peer", "proto", "secure"),
    [
        (PROXY, "https", True),     # the trusted proxy says https
        (PROXY, "http", False),     # the trusted proxy says http
        (PROXY, None, False),       # the trusted proxy says nothing: the scheme seen
        (LAN, "https", False),      # anyone else saying https is ignored
        (LAN, None, False),
    ],
)
def test_the_scheme_is_believed_only_from_a_trusted_proxy(client, peer, proto, secure):
    enrol(client)
    headers = {"X-Forwarded-Proto": proto} if proto else {}
    resp = client.post(
        "/api/fleet/nodes/sparky/update", json={"password": "hunter2"}, headers=headers
    )
    assert "hunter2" not in resp.text
    if secure:
        assert resp.status_code == 200, resp.text
        client.post(f"/api/fleet/runs/{resp.json()['id']}/stop")
    else:
        assert resp.status_code == 400, resp.text


@pytest.mark.parametrize("peer", [LAN])
def test_a_forged_header_is_refused_and_says_why(client, calls, caplog):
    """The end-to-end refusal: a password from outside the trusted set, with
    the header forged, never reaches a Spark or a log line, and the refusal
    names TRUSTED_PROXIES rather than claiming the page is not on HTTPS."""
    enrol(client)
    before = len(calls)
    caplog.set_level("DEBUG")
    resp = client.post(
        "/api/fleet/nodes/sparky/update",
        json={"password": "hunter2"},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert resp.status_code == 400
    assert "TRUSTED_PROXIES" in resp.json()["detail"]
    assert "hunter2" not in resp.text
    assert len(calls) == before, "a refused password still reached a Spark"
    assert "hunter2" not in caplog.text


@pytest.mark.parametrize("peer", [LAN])
def test_plain_http_without_a_header_keeps_the_plain_refusal(client):
    enrol(client)
    resp = client.post("/api/fleet/nodes/sparky/update", json={"password": "hunter2"})
    assert resp.status_code == 400
    assert "only accepted over HTTPS" in resp.json()["detail"]
    assert "TRUSTED_PROXIES" not in resp.json()["detail"]


def test_trusted_proxies_defaults_to_loopback(monkeypatch):
    """Unset, or "" as `${TRUSTED_PROXIES:-}` hands it over: loopback only,
    which inside a container is the container itself."""
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    assert Settings().trusted_proxies == "127.0.0.1,::1"
    monkeypatch.setenv("TRUSTED_PROXIES", "")
    assert Settings().trusted_proxies == "127.0.0.1,::1"
    monkeypatch.setenv("TRUSTED_PROXIES", "172.30.0.0/24")
    assert Settings().trusted_proxies == "172.30.0.0/24"


# ------------------------------------------------------------------- health


def test_health_reports_the_embedded_updater(client):
    body = client.get("/health").json()
    assert body["fleet_updates"] == "ok"
    assert "dashboards_paused" not in body, "nothing is paused, so nothing should be claimed"


def test_the_switch_off_leaves_the_routes_refusing_but_the_switch_alive(client):
    """The trap door from AL3d, guarded at route level as well as in the
    object: off is a state you must be able to leave."""
    assert client.post("/api/fleet/enabled", json={"enabled": False}).status_code == 200
    assert client.get("/api/fleet").json()["configured"] is False
    assert client.post("/api/fleet/check").status_code == 404
    assert client.get("/health").json()["fleet_updates"] == "not configured"
    assert client.post("/api/fleet/enabled", json={"enabled": True}).status_code == 200
    assert client.get("/api/fleet").json()["configured"] is True


def test_the_state_on_disk_is_enrolment_and_a_switch_and_nothing_else(client, tmp_path):
    """AL3d, from the outside: no host, no login, nothing that can go stale."""
    enrol(client)
    on_disk = json.loads((tmp_path / "state" / "fleet.json").read_text())
    assert on_disk["enrolled"] == ["sparky"]
    assert on_disk["enabled"] is True
    assert "nodes" not in on_disk and "units" not in on_disk
