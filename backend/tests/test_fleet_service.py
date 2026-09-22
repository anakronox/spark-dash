"""The fleet service as a library: no environment, no server, stoppable (AL3b).

Upstream this object could only exist once, configured by environment
variables read at import, with a stdlib HTTP server wrapped around it. The
split is what lets the backend own it — so these tests are mostly about the
things that were impossible before: build two on different directories, start
and stop the scheduler, and never touch a Spark.

`ssh.run` is faked throughout. Nothing here opens a connection, which is the
point: every guard in the state machine can be exercised without hardware.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from spark_dash_backend.fleet import ssh
from spark_dash_backend.fleet.service import Service


@pytest.fixture
def unreachable(monkeypatch):
    """Every Spark refuses to answer. The cheapest honest fake."""
    calls = []

    def fake_run(host, command, *, user=None, stdin=None, timeout=60):
        calls.append((host, command))
        return ssh.Result(255, "", "ssh: connect to host port 22: No route to host")

    monkeypatch.setattr(ssh, "run", fake_run)
    return calls


def test_two_services_on_two_directories_do_not_share_state(tmp_path):
    """The whole point of the constructor taking a path. Upstream, DATA was a
    module global resolved at import, so a second instance silently wrote into
    the first one's directory."""
    a = Service(tmp_path / "a", interval_min=60)
    b = Service(tmp_path / "b", interval_min=5)
    a.inv.add("sparky", "192.168.50.61")
    assert [n["name"] for n in a.inv.nodes] == ["sparky"]
    assert b.inv.nodes == []
    assert b.interval_min == 5
    assert (tmp_path / "a" / "fleet.json").exists()
    assert not (tmp_path / "b" / "fleet.json").exists()


def test_the_recipes_are_seeded_from_the_package(tmp_path):
    """They ship inside the wheel (AL3a) and are copied out on first start, so
    a fresh data directory can score a node before any Spark has been asked."""
    svc = Service(tmp_path, interval_min=60)
    seeded = list((tmp_path / "recipes").glob("spark-ota-*.json"))
    assert len(seeded) == 11
    assert svc._carried_recipe_hashes()


def test_the_scheduler_starts_stops_and_does_not_outlive_the_call(tmp_path, unreachable):
    """AL3c hangs the app's lifespan on this. A scheduler that only slept would
    take up to an hour to notice a shutdown; it waits on an Event instead."""
    svc = Service(tmp_path, interval_min=60)
    svc.inv.add("sparky", "192.168.50.61")
    svc.start()
    assert svc._scheduler is not None and svc._scheduler.is_alive()
    first = svc._scheduler
    svc.start()  # idempotent: the lifespan may run twice under a reload
    assert svc._scheduler is first, "a second start spawned a second scheduler"

    t0 = time.monotonic()
    svc.stop(timeout=5)
    assert time.monotonic() - t0 < 5, "stop waited for the interval instead of the event"
    assert not svc._scheduler.is_alive()
    # and it did its first sweep on the way past, against the fake
    assert unreachable, "the scheduler never reached a node"


def test_an_unreachable_spark_becomes_a_record_and_not_an_exception(tmp_path, unreachable):
    """A fleet where one box is off must still render. The error is carried on
    the record, clipped, with reachable false."""
    svc = Service(tmp_path, interval_min=60)
    node = svc.inv.add("sparky", "192.168.50.61")
    rec = svc.collect_node(node, reason="test")
    assert rec["reachable"] is False
    assert "No route to host" in rec["error"]
    assert json.loads((tmp_path / "posture" / "sparky.json").read_text())["name"] == "sparky"


def test_the_fleet_envelope_survives_a_fleet_that_has_never_been_reached(tmp_path, unreachable):
    """`fleet()` is what the panel renders. It must answer before any check has
    succeeded, because that is exactly when someone is looking at it."""
    svc = Service(tmp_path, interval_min=60)
    svc.inv.add("sparky", "192.168.50.61")
    svc.inv.add("sparketa", "192.168.50.62")
    env = svc.fleet()
    assert [n["name"] for n in env["nodes"]] == ["sparky", "sparketa"]
    assert env["interval_min"] == 60
    assert env["latest"]["name"].startswith("OTA")
    assert all(n["reachable"] is None for n in env["nodes"])
    assert env["last_sweep"] is None


def test_a_node_is_collected_once_at_a_time(tmp_path, unreachable):
    """The per-node lock. Two checks of one Spark must not open two sessions —
    upstream this mattered because the GUI's check button and the hourly sweep
    could land together; here the panel polls at 3s while checking."""
    svc = Service(tmp_path, interval_min=60)
    node = svc.inv.add("sparky", "192.168.50.61")
    threads = [threading.Thread(target=svc.collect_node, args=(node, "test")) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(unreachable) == 4, "each collect runs, serialised — not deduplicated"
    assert svc.checking == set(), "the checking flag leaked"


def test_configure_puts_the_key_and_known_hosts_on_the_command_line(monkeypatch):
    """AL4.3: uid 10002 has no home, so ssh needs telling where known_hosts is
    or accept-new fails on first contact with every Spark."""
    seen = {}

    def fake_subprocess_run(argv, **kw):
        seen["argv"] = argv

        class R:
            returncode, stdout, stderr = 0, "", ""

        return R()

    monkeypatch.setattr(ssh.subprocess, "run", fake_subprocess_run)
    ssh.configure(user="brian", key="/ssh/id_ed25519", known_hosts="/data/fleet/known_hosts")
    try:
        ssh.run("192.168.50.61", "true")
        argv = seen["argv"]
        assert "brian@192.168.50.61" in argv
        assert "UserKnownHostsFile=/data/fleet/known_hosts" in argv
        assert argv[argv.index("-i") + 1] == "/ssh/id_ed25519"
        assert "IdentitiesOnly=yes" in argv
    finally:
        ssh.configure()  # module state: leave it as it was found
