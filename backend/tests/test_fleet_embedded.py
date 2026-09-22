"""The fleet updater running in-process (AL3c).

Three of these are guards on traps written down in AL4 before any of this was
built, and each one fails loudly if the thing it guards is undone:

  AL4.5  a Spark is thirty to sixty seconds of SSH, and this process also
         serves a live WebSocket to every open dashboard
  AL4.1  the backend restarts on every deploy, and a run that dies mid-flight
         must not leave a Spark's own Dashboard updater paused
  AL2    the sudo password is never rendered anywhere it was not typed

No Spark is touched: `ssh.run` is faked throughout.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from spark_dash_backend.fleet import ssh
from spark_dash_backend.fleet_api import FleetError
from spark_dash_backend.fleet_embedded import EmbeddedFleet


def make(tmp_path, **kw) -> EmbeddedFleet:
    key = tmp_path / "id_ed25519"
    key.write_text("not a real key")
    fleet = EmbeddedFleet(
        state_dir=tmp_path / "state",
        ssh_user="brian",
        ssh_key=key,
        interval_min=60,
        **kw,
    )
    fleet.start()
    return fleet


@pytest.fixture(autouse=True)
def _no_ssh(monkeypatch):
    """Nothing reaches a node unless a test says how."""
    monkeypatch.setattr(
        ssh, "run", lambda *a, **k: ssh.Result(255, "", "ssh: no route to host")
    )
    yield
    ssh.configure()  # module state: leave it as found


# ---------------------------------------------------------------- readiness


def test_requirements_name_what_is_missing_rather_than_saying_not_configured(tmp_path):
    """AL5 promised the Settings warning would say WHICH piece is absent, so
    the readiness check is per-item and not a boolean."""
    fleet = EmbeddedFleet(state_dir=tmp_path / "s", ssh_user="", ssh_key=tmp_path / "nope")
    reqs = fleet.requirements()
    assert reqs["ssh_key_mounted"] is False
    assert reqs["ssh_user_set"] is False
    assert reqs["state_dir_writable"] is True
    assert fleet.configured is False


def test_an_unconfigured_fleet_starts_the_app_anyway_and_offers_nothing(tmp_path):
    """A dashboard with no key mounted must boot normally. The feature being
    off is not an error; it is the default."""
    fleet = EmbeddedFleet(state_dir=tmp_path / "s", ssh_user="", ssh_key=None)
    fleet.start()
    assert fleet.svc is None
    with pytest.raises(FleetError) as exc:
        asyncio.run(fleet.envelope(secure=True))
    assert exc.value.status == 404
    assert "ssh_key_mounted" in exc.value.detail


# ------------------------------------------------------------- AL4.5: loop


async def test_a_slow_spark_does_not_block_the_event_loop(tmp_path, monkeypatch):
    """THE TRAP: this process serves the live WebSocket. A collect called
    straight from an `async def` freezes the whole dashboard for every viewer,
    not just the fleet panel. Everything that reaches a Spark goes through
    asyncio.to_thread, and this fails if that is ever undone."""
    fleet = make(tmp_path)
    monkeypatch.setattr(fleet.svc, "fleet", lambda: (time.sleep(0.3), {"nodes": []})[1])

    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    await fleet.envelope(secure=False)
    during = ticks
    beat.cancel()
    # ~30 if the loop kept running, ~1 if the call blocked it
    assert during >= 10, f"the event loop was blocked: only {during} ticks during a 0.3s call"


# --------------------------------------------------------- AL4.1: reconcile


def paused_run(fleet: EmbeddedFleet, node: str, before: dict) -> None:
    """A run record left behind by a process that died holding a pause."""
    d = fleet.svc.data / "runs" / "20260905T180000Z-sparkjr"
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(
        json.dumps(
            {
                "id": "20260905T180000Z-sparkjr",
                "status": "failed",
                "message": "the controller restarted during this run",
                "nodes": [{"name": node, "status": "failed", "dashboard_settings_before": before}],
            }
        )
    )


def test_a_dashboard_left_paused_by_a_dead_run_is_resumed_on_start(tmp_path, monkeypatch):
    """The 2026-09-05 bug, reintroduced by embedding: the backend restarts on
    every deploy, the Run thread dies with it, and the `finally` that resumes
    the Spark's Dashboard never runs."""
    fleet = make(tmp_path)
    fleet.svc.inv.add("sparkjr", "192.168.50.63")
    paused_run(fleet, "sparkjr", {"absent": True})

    sent = []
    monkeypatch.setattr(
        ssh, "run", lambda host, cmd, **k: (sent.append((host, cmd)), ssh.Result(0, "", ""))[1]
    )
    stranded = fleet.svc.resume_paused_dashboards()

    assert stranded == []
    assert sent and sent[0][0] == "192.168.50.63"
    assert "rm -f /opt/nvidia/dgx-dashboard/settings.json" in sent[0][1]
    assert "sudo -n" in sent[0][1], "the recovery path has no password and must not pretend to"
    record = fleet.svc.data / "runs" / "20260905T180000Z-sparkjr" / "state.json"
    state = json.loads(record.read_text())
    left = state["nodes"][0]["dashboard_settings_before"]
    assert left is None, "not marked done: the resume would retry forever"


def test_the_previous_settings_are_restored_byte_for_byte_not_just_enabled(tmp_path, monkeypatch):
    """A Spark where someone CHOSE to disable updates must stay disabled. The
    pause records the bytes; the resume puts those bytes back."""
    fleet = make(tmp_path)
    fleet.svc.inv.add("sparkjr", "192.168.50.63")
    paused_run(fleet, "sparkjr", {"absent": False, "text": '{"update": {"enabled": false}}'})

    sent = []
    monkeypatch.setattr(
        ssh, "run", lambda host, cmd, **k: (sent.append(cmd), ssh.Result(0, "", ""))[1]
    )
    fleet.svc.resume_paused_dashboards()
    assert '{"update": {"enabled": false}}' in sent[0]


async def test_what_cannot_be_resumed_is_reported_and_not_swallowed(tmp_path):
    """Passwordless sudo is the only tool the recovery has, and a Spark that
    asks for a password will refuse it. Seventeen days of silence happened
    because nothing said so; this makes it say so."""
    fleet = make(tmp_path)
    fleet.svc.inv.add("sparkjr", "192.168.50.63")
    paused_run(fleet, "sparkjr", {"absent": True})

    stranded = fleet.svc.resume_paused_dashboards()
    assert [x["node"] for x in stranded] == ["sparkjr"]
    envelope = await fleet.envelope(secure=True)
    assert envelope["dashboards_stranded"][0]["node"] == "sparkjr", "the panel cannot see it"


def test_the_reconcile_runs_on_startup_and_is_not_merely_available(tmp_path, monkeypatch):
    """The trap is "every deploy", so being callable is not enough -- it has to
    be on the path a deploy takes. This seeds a paused run the way a restart
    would find one, then starts the thing the lifespan starts."""
    from spark_dash_backend.fleet.service import Service

    seed = Service(tmp_path / "state", interval_min=60)
    seed.inv.add("sparky", "192.168.50.61")
    d = seed.data / "runs" / "20260905T180000Z-sparky"
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(
        json.dumps({"id": "r", "status": "failed",
                    "nodes": [{"name": "sparky", "dashboard_settings_before": {"absent": True}}]})
    )

    sent = []
    monkeypatch.setattr(
        ssh, "run", lambda host, cmd, **k: (sent.append(cmd), ssh.Result(0, "", ""))[1]
    )
    fleet = make(tmp_path)          # start(), exactly as the app's lifespan calls it
    fleet.stop()
    assert sent, "starting the fleet updater did not look for a Dashboard it had left paused"
    assert "settings.json" in sent[0]


def test_a_corrupt_run_record_does_not_stop_the_updater_starting(tmp_path):
    """Recovery reads files written by a process that was killed. Some of them
    will be half-written."""
    fleet = make(tmp_path)
    d = fleet.svc.data / "runs" / "20260905T180000Z-truncated"
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text('{"id": "trunc", "nodes": [{"na')
    assert fleet.svc.resume_paused_dashboards() == []


async def test_health_says_a_spark_is_muted_rather_than_leaving_it_muted(tmp_path):
    """Seventeen days of silence happened because every surface said "fine".
    An uptime check has to be able to see this one."""
    from fastapi.testclient import TestClient

    from spark_dash_backend.app import create_app
    from spark_dash_backend.config import Settings

    key = tmp_path / "id_ed25519"
    key.write_text("not a real key")
    settings = Settings(
        fleet_updates_url="",
        fleet_ssh_user="brian",
        fleet_ssh_key=key,
        fleet_state_dir=tmp_path / "state",
        prometheus_url="http://nowhere:9090",
        alertmanager_url="http://nowhere:9093",
    )
    app = create_app(settings)
    fleet = app.state.fleet_updates
    assert fleet.embedded, "a backend with no FLEET_UPDATES_URL should run it in-process"

    fleet.start()
    fleet.svc.inv.add("sparkjr", "192.168.50.63")
    paused_run(fleet, "sparkjr", {"absent": True})
    fleet.svc.resume_paused_dashboards()  # the fake ssh refuses: it stays paused
    fleet.stop()

    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["dashboards_paused"] == ["sparkjr"]


# ------------------------------------------------------------ AL2: password


async def test_a_password_is_refused_off_tls_with_the_sentence(tmp_path):
    """The rule does not soften when the network hop disappears. Upstream the
    fleet service refused; embedded there is nothing behind us to refuse."""
    fleet = make(tmp_path)
    fleet.svc.inv.add("sparky", "192.168.50.61")
    with pytest.raises(FleetError) as exc:
        await fleet.node_action("sparky", "update", password="hunter2", secure=False)
    assert exc.value.status == 400
    assert "only accepted over HTTPS" in exc.value.detail
    assert "hunter2" not in exc.value.detail, "the refusal echoed the password back"


async def test_the_plain_password_opt_out_is_the_operators_to_make(tmp_path):
    """SPARK_FLEET_ALLOW_PLAIN_PASSWORD, ported. Off here by default and this
    project does not set it, but a LAN-only install may."""
    fleet = make(tmp_path, allow_plain_password=True)
    fleet.svc.inv.add("sparky", "192.168.50.61")
    state = await fleet.node_action("sparky", "update", password="hunter2", secure=False)
    assert state["status"] == "running"
    fleet.svc.runs[state["id"]].stop_requested = True


async def test_no_password_reaches_any_file_a_run_writes(tmp_path):
    """AL2's missing test. The password is held for one run and written
    nowhere -- not state.json, not a log line, not a step envelope."""
    fleet = make(tmp_path)
    fleet.svc.inv.add("sparky", "192.168.50.61")
    state = await fleet.node_action("sparky", "update", password="hunter2", secure=True)
    run = fleet.svc.runs[state["id"]]
    run.thread.join(timeout=15)  # holds at `check`: the fake ssh refuses

    written = list((fleet.svc.data / "runs").rglob("*"))
    assert written, "the run wrote nothing at all; this test would pass vacuously"
    for f in written:
        if f.is_file():
            assert "hunter2" not in f.read_text(errors="replace"), f"the password reached {f.name}"


async def test_an_unknown_node_is_a_404_and_a_busy_one_a_409(tmp_path):
    """The fleet service's own wording and status for each, kept."""
    fleet = make(tmp_path)
    with pytest.raises(FleetError) as exc:
        await fleet.node_action("nobody", "check", password=None, secure=True)
    assert exc.value.status == 404

    fleet.svc.inv.add("sparky", "192.168.50.61")
    first = await fleet.node_action("sparky", "update", password=None, secure=True)
    with pytest.raises(FleetError) as exc:
        await fleet.node_action("sparky", "update", password=None, secure=True)
    assert exc.value.status == 409
    assert "already being updated" in exc.value.detail
    fleet.svc.runs[first["id"]].stop_requested = True
