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

#: What the dashboard knows about each Spark (AL3d). The fleet holds ids; this
#: turns one into an address and a cluster.
CLUSTER_YML = {
    "sparky": {"name": "sparky", "host": "192.168.50.61", "cluster": None},
    "sparketa": {"name": "sparketa", "host": "192.168.50.62", "cluster": "danflashes"},
    "sparkjr": {"name": "sparkjr", "host": "192.168.50.63", "cluster": "danflashes"},
}
RESOLVE = CLUSTER_YML.get


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
    a = Service(tmp_path / "a", interval_min=60, resolve=RESOLVE)
    b = Service(tmp_path / "b", interval_min=5, resolve=RESOLVE)
    a.inv.add("sparky")
    assert [n["name"] for n in a.inv.nodes] == ["sparky"]
    assert b.inv.nodes == []
    assert b.interval_min == 5
    assert (tmp_path / "a" / "fleet.json").exists()
    assert not (tmp_path / "b" / "fleet.json").exists()


def test_the_recipes_are_seeded_from_the_package(tmp_path):
    """They ship inside the wheel (AL3a) and are copied out on first start, so
    a fresh data directory can score a node before any Spark has been asked."""
    svc = Service(tmp_path, interval_min=60, resolve=RESOLVE)
    seeded = list((tmp_path / "recipes").glob("spark-ota-*.json"))
    assert len(seeded) == 11
    assert svc._carried_recipe_hashes()


def test_the_scheduler_starts_stops_and_does_not_outlive_the_call(tmp_path, unreachable):
    """AL3c hangs the app's lifespan on this. A scheduler that only slept would
    take up to an hour to notice a shutdown; it waits on an Event instead."""
    svc = Service(tmp_path, interval_min=60, resolve=RESOLVE)
    svc.inv.add("sparky")
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
    svc = Service(tmp_path, interval_min=60, resolve=RESOLVE)
    node = svc.inv.add("sparky")
    rec = svc.collect_node(node, reason="test")
    assert rec["reachable"] is False
    assert "No route to host" in rec["error"]
    assert json.loads((tmp_path / "posture" / "sparky.json").read_text())["name"] == "sparky"


def test_the_fleet_envelope_survives_a_fleet_that_has_never_been_reached(tmp_path, unreachable):
    """`fleet()` is what the panel renders. It must answer before any check has
    succeeded, because that is exactly when someone is looking at it."""
    svc = Service(tmp_path, interval_min=60, resolve=RESOLVE)
    svc.inv.add("sparky")
    svc.inv.add("sparketa")
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
    svc = Service(tmp_path, interval_min=60, resolve=RESOLVE)
    node = svc.inv.add("sparky")
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


# ------------------------------------------------- AL3d: one list of Sparks


def test_a_host_cannot_differ_because_there_is_only_one_of_it(tmp_path):
    """The state AK7's checkbox had to warn about is now unreachable. The fleet
    keeps an id; the address is read from cluster.yml every time it is used, so
    editing cluster.yml moves the fleet with it."""
    moved = dict(CLUSTER_YML)
    svc = Service(tmp_path, interval_min=60, resolve=lambda n: moved.get(n))
    svc.inv.add("sparky")
    assert svc.inv.get("sparky")["host"] == "192.168.50.61"
    moved["sparky"] = {"name": "sparky", "host": "10.0.0.9", "cluster": None}
    assert svc.inv.get("sparky")["host"] == "10.0.0.9"
    assert json.loads((tmp_path / "fleet.json").read_text())["enrolled"] == ["sparky"]


def test_pairing_comes_from_cluster_yml_and_not_from_a_second_declaration(tmp_path):
    """`cluster:` already means "these pool memory", which is exactly the thing
    that must end an update on the same release. A standalone Spark is a unit
    of one; an unenrolled cluster member is not dragged in."""
    svc = Service(tmp_path, interval_min=60, resolve=RESOLVE)
    svc.inv.add("sparketa")
    assert svc.inv.unit_of("sparketa") == ["sparketa"], "sparkjr is not enrolled yet"
    svc.inv.add("sparkjr")
    assert svc.inv.unit_of("sparketa") == ["sparketa", "sparkjr"]
    assert svc.inv.unit_of("sparkjr") == ["sparketa", "sparkjr"]
    svc.inv.add("sparky")
    assert svc.inv.unit_of("sparky") == ["sparky"], "a standalone Spark updates alone"


def test_enrolling_something_the_dashboard_never_heard_of_is_refused(tmp_path):
    """Upstream this took a name and a host and believed both. Now the only
    Sparks that can be enrolled are ones cluster.yml describes."""
    svc = Service(tmp_path, interval_min=60, resolve=RESOLVE)
    with pytest.raises(ValueError, match="not a node this dashboard knows"):
        svc.inv.add("a-machine-that-does-not-exist")


def test_a_node_dropped_from_cluster_yml_becomes_an_orphan_not_a_silence(tmp_path):
    """Its posture and run records are still on disk. Someone has to decide
    whether that was a rename or a removal, so it is reported rather than
    quietly skipped."""
    known = dict(CLUSTER_YML)
    svc = Service(tmp_path, interval_min=60, resolve=lambda n: known.get(n))
    svc.inv.add("sparky")
    svc.inv.add("sparketa")
    del known["sparky"]
    assert [n["name"] for n in svc.inv.nodes] == ["sparketa"]
    assert svc.inv.orphans == ["sparky"]
    assert svc.fleet()["orphans"] == ["sparky"]


def test_the_cabling_disagreeing_with_the_config_is_said_out_loud(tmp_path):
    """Before AL3d the fabric silently decided, so this could not happen. Now
    cluster.yml decides and the fabric is evidence -- and a cabled pair the
    config does not group would be updated apart, which breaks the model that
    spans them."""
    svc = Service(tmp_path, interval_min=60, resolve=RESOLVE)
    svc.inv.add("sparky")
    svc.inv.add("sparketa")
    # the fabric says these two are cabled together; cluster.yml does not agree
    svc.inv.set_clusters([{"members": ["sparky", "sparketa"], "links": []}])
    found = svc.fabric_disagreements()
    assert found and found[0]["cabled"] == ["sparketa", "sparky"]
    assert svc.fleet()["fabric_disagreements"] == found

    # and when they do agree, it says nothing
    svc.inv.add("sparkjr")
    svc.inv.set_clusters([{"members": ["sparketa", "sparkjr"], "links": []}])
    assert svc.fabric_disagreements() == []


def test_an_old_fleet_json_is_migrated_rather_than_retyped(tmp_path):
    """The live deployment has one of these, with three Sparks in it."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "fleet.json").write_text(json.dumps({
        "nodes": [{"name": "sparky", "host": "192.168.50.61", "added_at": "2026-09-05T00:00:00Z"},
                  {"name": "sparketa", "host": "192.168.50.62"}],
        "units": [["sparketa", "sparkjr"]],
        "cluster_names": {"sparketa+sparkjr": "danflashes"},
        "clusters": [],
    }))
    svc = Service(tmp_path, interval_min=60, resolve=RESOLVE)
    assert svc.inv.enrolled == ["sparky", "sparketa"]
    on_disk = json.loads((tmp_path / "fleet.json").read_text())
    assert "nodes" not in on_disk and "units" not in on_disk
    assert on_disk["enrolled"] == ["sparky", "sparketa"]
    # the declared unit is gone, and cluster.yml gives the same answer anyway
    svc.inv.add("sparkjr")
    assert svc.inv.unit_of("sparketa") == ["sparketa", "sparkjr"]
