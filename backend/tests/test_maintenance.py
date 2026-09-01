"""Maintenance windows: a silence with a name, declared before the work.

The traps are the ones a silence-backed design invites: two silences that must
live and die together, a comment that has to be readable AND parseable, and a
record in Prometheus that must say "during maintenance" without ever saying
"did not fire".
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from spark_dash_backend.alert_history import AlertEpisode, summarise
from spark_dash_backend.inventory import Node
from spark_dash_backend.maintenance import (
    AUTHOR,
    METRIC,
    MaintenanceService,
    ScopeError,
    comment_for,
    extract_intervals,
    matcher_sets,
    parse_comment,
    render_metrics,
    resolve_scope,
    tag_episodes,
    windows_from_silences,
)
from spark_dash_common.models import ClusterSnapshot, NodeSnapshot

INVENTORY = [
    Node("sparky", "10.0.0.1"),
    Node("gx10-2", "10.0.0.2", cluster="danflashes"),
    Node("gx10-3", "10.0.0.3", cluster="danflashes"),
]


# ------------------------------------------------------------------ scope


def test_node_scope_covers_one_node_and_no_peers_when_standalone():
    r = resolve_scope("node", "sparky", INVENTORY)
    assert r.nodes == ["sparky"]
    sets = matcher_sets(r)
    assert len(sets) == 1
    peers, matchers = sets[0]
    assert peers is False
    assert matchers == [{"name": "node", "value": "sparky", "isRegex": False, "isEqual": True}]


def test_node_scope_in_a_cluster_also_mutes_the_peer_comparisons():
    """Decision 1: the peer alerts fire on the node NOBODY touched, because the
    idle one under maintenance makes the working one read hot."""
    r = resolve_scope("node", "gx10-2", INVENTORY)
    sets = matcher_sets(r)
    assert [peers for peers, _ in sets] == [False, True]
    _, peer_matchers = sets[1]
    by_name = {m["name"]: m for m in peer_matchers}
    assert by_name["alertname"]["isRegex"] is True
    assert by_name["alertname"]["value"] == "ClusterNode.*"
    assert by_name["cluster"] == {
        "name": "cluster",
        "value": "danflashes",
        "isRegex": False,
        "isEqual": True,
    }


def test_cluster_scope_covers_every_member_in_one_regex():
    r = resolve_scope("cluster", "danflashes", INVENTORY)
    assert r.nodes == ["gx10-2", "gx10-3"]
    sets = matcher_sets(r)
    assert len(sets) == 1, "the node regex already covers the peer alerts"
    (_, matchers), = sets
    assert matchers[0]["isRegex"] is True
    # Escaped, so a hyphen or dot in an id cannot widen the match.
    assert matchers[0]["value"] == r"gx10\-2|gx10\-3"


@pytest.mark.parametrize("scope,name", [("node", "nope"), ("cluster", "nope"), ("rack", "x")])
def test_unknown_scope_is_an_error_not_an_empty_silence(scope, name):
    """An empty matcher set would silence NOTHING while reporting a window."""
    with pytest.raises(ScopeError):
        resolve_scope(scope, name, INVENTORY)


# ---------------------------------------------------------------- comment


@pytest.mark.parametrize("reason", ["", "trying Qwen3-235B on vLLM", "two\nlines  of   text"])
def test_comment_round_trips(reason):
    r = resolve_scope("node", "gx10-2", INVENTORY)
    for peers in (False, True):
        text = comment_for(r, reason, "1a2b3c4d", peers=peers)
        parsed = parse_comment(text)
        assert parsed is not None, text
        assert parsed.scope == "node"
        assert parsed.name == "gx10-2"
        assert parsed.peers is peers
        assert parsed.window == "1a2b3c4d"
        assert parsed.reason == " ".join(reason.split())


def test_comment_reads_as_a_sentence_on_alertmanagers_own_ui():
    r = resolve_scope("cluster", "danflashes", INVENTORY)
    assert (
        comment_for(r, "reloading the 70B", "9f8e7d6c", peers=False)
        == "maintenance cluster danflashes: reloading the 70B [window 9f8e7d6c]"
    )


def test_a_hand_written_comment_is_not_a_window():
    assert parse_comment("Silenced from the dashboard") is None
    assert parse_comment(None) is None


# ---------------------------------------------------------------- windows


def silence(sid, comment, matchers, *, author=AUTHOR, ends="2026-09-02T18:00:00.000Z"):
    return {
        "id": sid,
        "createdBy": author,
        "comment": comment,
        "matchers": matchers,
        "startsAt": "2026-09-02T14:00:00.000Z",
        "endsAt": ends,
        "status": {"state": "active"},
    }


def held(fingerprint, *silence_ids):
    return {"fingerprint": fingerprint, "status": {"silencedBy": list(silence_ids)}}


def test_two_silences_group_back_into_one_window():
    r = resolve_scope("node", "gx10-2", INVENTORY)
    (_, primary), (_, peers) = matcher_sets(r)
    silences = [
        silence("s1", comment_for(r, "swap", "abcd0001", peers=False), primary),
        silence("s2", comment_for(r, "swap", "abcd0001", peers=True), peers),
    ]
    windows = windows_from_silences(silences)
    assert len(windows) == 1
    w = windows[0]
    assert w.id == "abcd0001"
    assert w.scope == "node"
    assert w.name == "gx10-2"
    assert w.nodes == ["gx10-2"]
    assert w.reason == "swap"
    assert sorted(w.silence_ids) == ["s1", "s2"]


def test_cluster_window_reads_its_members_back_from_the_regex():
    r = resolve_scope("cluster", "danflashes", INVENTORY)
    (_, primary), = matcher_sets(r)
    (w,) = windows_from_silences(
        [silence("s1", comment_for(r, "", "abcd0002", peers=False), primary)]
    )
    assert w.nodes == ["gx10-2", "gx10-3"]


def test_held_counts_distinct_alerts_across_the_windows_silences():
    """One alert matched by both the node silence and the peers silence is ONE
    alert held, not two."""
    r = resolve_scope("node", "gx10-2", INVENTORY)
    (_, primary), (_, peers) = matcher_sets(r)
    silences = [
        silence("s1", comment_for(r, "", "abcd0003", peers=False), primary),
        silence("s2", comment_for(r, "", "abcd0003", peers=True), peers),
    ]
    holding = [held("fp1", "s1"), held("fp2", "s1", "s2"), held("fp3", "other")]
    w, = windows_from_silences(silences, holding)
    assert w.held == 2


def test_a_plain_silence_and_a_foreign_author_are_not_windows():
    r = resolve_scope("node", "sparky", INVENTORY)
    (_, primary), = matcher_sets(r)
    silences = [
        silence("s1", "Silenced from the dashboard", primary, author="spark-dash"),
        # Right grammar, wrong author: someone typed it into :9093 by hand.
        silence("s2", comment_for(r, "", "abcd0004", peers=False), primary, author="brian"),
    ]
    assert windows_from_silences(silences) == []


def test_an_orphaned_peers_silence_is_not_a_window():
    """Primary gone, peers silence left: nothing protects the node, so the
    page must not claim a window. It stays visible as a plain silence."""
    r = resolve_scope("node", "gx10-2", INVENTORY)
    _, (_, peers) = matcher_sets(r)
    assert windows_from_silences(
        [silence("s2", comment_for(r, "", "abcd0005", peers=True), peers)]
    ) == []


# ---------------------------------------------------------------- metrics


def test_metrics_render_one_series_per_covered_node_without_the_reason():
    r = resolve_scope("cluster", "danflashes", INVENTORY)
    (_, primary), = matcher_sets(r)
    w, = windows_from_silences(
        [silence("s1", comment_for(r, 'a "quoted" reason', "abcd0006", peers=False), primary)]
    )
    text = render_metrics([w])
    assert f"# TYPE {METRIC} gauge" in text
    for node in ("gx10-2", "gx10-3"):
        labels = f'node="{node}",scope="cluster",name="danflashes",window="abcd0006"'
        expected = f"{METRIC}{{{labels}}} 1"
        assert expected in text
    # Free text is not a label: a new value per window is a new series per window.
    assert "quoted" not in text


def test_metrics_render_an_empty_family_when_nothing_is_on():
    text = render_metrics([])
    assert text.endswith("gauge\n")


# ---------------------------------------------------------------- record


@dataclass
class FakeSeries:
    labels: dict
    points: list


def maint_series(node, samples, *, window="w1", scope="node", name=None):
    return FakeSeries(
        labels={"node": node, "scope": scope, "name": name or node, "window": window},
        points=[(ts, 1.0) for ts in samples],
    )


def test_intervals_split_on_gaps_and_flag_ongoing():
    t0 = 1_000_000
    first = [t0 + i * 15 for i in range(10)]
    second = [t0 + 3600 + i * 15 for i in range(4)]
    series = [maint_series("sparky", first + second)]
    ivs = extract_intervals(series, window_end=t0 + 3600 + 45, gap_tolerance_s=150)
    assert [(iv.started_at, iv.ended_at, iv.ongoing) for iv in ivs] == [
        (t0, t0 + 135, False),
        (t0 + 3600, t0 + 3645, True),
    ]


def episode(node, start, end, *, fired=True):
    return AlertEpisode(
        alertname="RouterUnreachable",
        severity="warning",
        node=node,
        started_at=start,
        ended_at=end,
        ongoing=False,
        fired=fired,
        fired_at=start if fired else None,
        labels={},
    )


def test_episodes_overlapping_a_window_on_their_node_are_tagged_not_dropped():
    t0 = 1_000_000
    ivs = extract_intervals(
        [maint_series("sparky", [t0, t0 + 60, t0 + 120])],
        window_end=t0 + 10_000,
        gap_tolerance_s=150,
    )
    eps = [
        episode("sparky", t0 + 30, t0 + 500),  # inside
        episode("sparky", t0 + 5000, t0 + 5100),  # after
        episode("gx10-2", t0 + 30, t0 + 500),  # other node, same time
        episode(None, t0 + 30, t0 + 500),  # no node label: never maintenance
    ]
    tag_episodes(eps, ivs)
    assert [e.maintenance for e in eps] == [True, False, False, False]
    assert len(eps) == 4, "tagging must never remove an episode"
    s = summarise(eps)
    assert s["fired"] == 4
    assert s["during_maintenance"] == 1
    assert eps[0].as_dict()["maintenance"] is True


# ---------------------------------------------------------------- service


class FakeAlertmanager:
    def __init__(self, *, fail_on_second_create=False):
        self.silences_store: dict[str, dict] = {}
        self.expired: list[str] = []
        self.created: list[dict] = []
        self._n = 0
        self._fail_on_second = fail_on_second_create

    async def silences(self):
        return [s for s in self.silences_store.values() if s["status"]["state"] == "active"]

    async def silenced(self):
        return []

    async def create_silence(self, matchers, *, hours, comment, author="spark-dash"):
        self._n += 1
        if self._fail_on_second and self._n == 2:
            raise RuntimeError("alertmanager exploded")
        sid = f"s{self._n}"
        self.silences_store[sid] = silence(sid, comment, matchers, author=author)
        self.created.append({"id": sid, "hours": hours, "comment": comment, "author": author})
        return sid

    async def expire_silence(self, sid):
        self.expired.append(sid)
        if sid in self.silences_store:
            self.silences_store[sid]["status"]["state"] = "expired"


class FakeInventory:
    def nodes(self, now=None):
        return INVENTORY


async def test_start_creates_both_silences_under_one_window_with_the_author():
    am = FakeAlertmanager()
    svc = MaintenanceService(am, FakeInventory())
    w = await svc.start("node", "gx10-2", hours=4, reason="swap")
    assert len(am.created) == 2
    assert {c["author"] for c in am.created} == {AUTHOR}
    assert {c["hours"] for c in am.created} == {4}
    assert w.scope == "node" and w.name == "gx10-2"
    assert sorted(w.silence_ids) == ["s1", "s2"]
    # Visible immediately, not after a TTL.
    assert [x.id for x in svc.cached] == [w.id]


async def test_half_a_window_is_rolled_back():
    """Node muted, peers not, page showing one window: worse than nothing."""
    am = FakeAlertmanager(fail_on_second_create=True)
    svc = MaintenanceService(am, FakeInventory())
    with pytest.raises(RuntimeError):
        await svc.start("node", "gx10-2", hours=4)
    assert am.expired == ["s1"]


async def test_end_expires_every_silence_in_the_window():
    am = FakeAlertmanager()
    svc = MaintenanceService(am, FakeInventory())
    w = await svc.start("node", "gx10-2", hours=4)
    await svc.end(w.id)
    assert sorted(am.expired) == ["s1", "s2"]
    assert svc.cached == []


async def test_end_of_an_unknown_window_is_a_key_error():
    svc = MaintenanceService(FakeAlertmanager(), FakeInventory())
    with pytest.raises(KeyError):
        await svc.end("nope")


async def test_stamp_marks_covered_nodes_and_leaves_health_alone():
    am = FakeAlertmanager()
    svc = MaintenanceService(am, FakeInventory())
    await svc.start("cluster", "danflashes", hours=2, reason="reload")
    snap = ClusterSnapshot(
        ts=datetime.now(UTC),
        nodes=[
            NodeSnapshot(node_id="sparky", ts=datetime.now(UTC)),
            NodeSnapshot(
                node_id="gx10-2",
                ts=datetime.now(UTC),
                up=False,
                health="critical",
                health_reasons=["unreachable"],
            ),
            NodeSnapshot(node_id="gx10-3", ts=datetime.now(UTC)),
        ],
    )
    svc.stamp(snap)
    by_id = {n.node_id: n for n in snap.nodes}
    assert by_id["sparky"].maintenance is None
    assert by_id["gx10-2"].maintenance is not None
    assert by_id["gx10-2"].maintenance.name == "danflashes"
    assert by_id["gx10-2"].maintenance.reason == "reload"
    assert by_id["gx10-2"].health == "critical", "the mark de-escalates presentation, not the fact"
    assert by_id["gx10-3"].maintenance is not None
    # Serialises: the WebSocket frame is model_dump_json.
    assert "danflashes" in snap.model_dump_json()
    await asyncio.sleep(0)  # let any background refresh settle
