"""REST surface, with Prometheus and the agents stubbed.

Runs anywhere — no cluster, no Prometheus, no GPU.
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from spark_dash_backend.app import create_app
from spark_dash_backend.config import Settings
from spark_dash_common.models import (
    ClusterSnapshot,
    GpuMetrics,
    HealthState,
    LlamaRouterMetrics,
    MemoryMetrics,
    ModelState,
    NodeSnapshot,
    RouterModel,
    Runtimes,
    VllmMetrics,
)

INVENTORY = """
- targets: ['192.168.50.61:9500']
  labels: {node: gx10-1}
- targets: ['192.168.50.62:9500']
  labels: {node: gx10-2}
"""


def node(node_id: str, *, up: bool = True, util: float = 50.0) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=node_id,
        ts=datetime.now(UTC),
        up=up,
        health=HealthState.GOOD if up else HealthState.CRITICAL,
        gpu=GpuMetrics(util_pct=util) if up else None,
        memory=(
            MemoryMetrics(
                total_bytes=128_000_000_000,
                available_bytes=100_000_000_000,
                used_bytes=28_000_000_000,
                unified=True,
            )
            if up
            else None
        ),
        runtimes=Runtimes(
            llama_cpp=[
                LlamaRouterMetrics(
                    endpoint="http://192.168.50.61:8001",
                    name="192.168.50.61:8001",
                    models=[
                        RouterModel(
                            name="qwen36-35b",
                            state=ModelState.ACTIVE,
                            raw_status="loaded",
                            tokens_per_sec=41.2,
                        ),
                        RouterModel(
                            name="cydonia-24b",
                            state=ModelState.SLEEPING,
                            raw_status="sleeping",
                        ),
                    ],
                    max_instances=3,
                    tokens_per_sec=41.2,
                )
            ],
            vllm=[VllmMetrics(model="llama-3.3-70b", tokens_per_sec=88.5)],
        )
        if up
        else Runtimes(),
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def fake_poll_once(self):
        snap = ClusterSnapshot(
            ts=datetime.now(UTC), nodes=[node("gx10-1"), node("gx10-2", up=False)]
        )
        self._latest = snap
        return snap

    async def fake_healthy(self):
        return True

    monkeypatch.setattr("spark_dash_backend.poller.LivePoller.poll_once", fake_poll_once)
    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.healthy", fake_healthy)

    # Env-driven inventory: the path production actually uses.
    app = create_app(
        Settings(
            spark_nodes="gx10-1=192.168.50.61,gx10-2=192.168.50.62",
            prometheus_targets_dir=tmp_path,
            static_dir=tmp_path / "nostatic",
        )
    )
    with TestClient(app) as c:
        yield c


def test_nodes_lists_inventory_with_liveness(client):
    body = client.get("/api/nodes").json()
    assert [n["node_id"] for n in body["nodes"]] == ["gx10-1", "gx10-2"]
    assert body["nodes"][0]["up"] is True
    assert body["nodes"][1]["up"] is False


def test_cluster_summary_aggregates(client):
    body = client.get("/api/cluster/summary").json()
    assert body["nodes_total"] == 2
    assert body["nodes_up"] == 1
    # 41.2 from llama.cpp + 88.5 from vLLM on the one live node.
    assert body["tokens_per_second"] == pytest.approx(129.7)


def test_largest_free_block_is_per_group_not_a_total(client):
    """The fixture's two nodes are ungrouped, so each is its own group. The
    answer to "can I load this" is the best single group can offer — summing
    them would describe capacity that doesn't exist, since a model can't span
    machines that aren't clustered."""
    body = client.get("/api/cluster/summary").json()
    # Only gx10-1 is up, with 100GB free. gx10-2 is down and contributes none.
    assert body["largest_free_block_bytes"] == 100_000_000_000
    assert body["largest_free_block_group"] == "gx10-1"


def test_ungrouped_nodes_are_each_their_own_group(client):
    body = client.get("/api/cluster/summary").json()
    groups = {g["key"]: g for g in body["groups"]}
    assert set(groups) == {"gx10-1", "gx10-2"}
    assert all(g["standalone"] for g in groups.values())
    assert all(g["group"] is None for g in groups.values())


def test_grouped_nodes_pool_their_memory(tmp_path, monkeypatch):
    """Clustered nodes do distributed inference, so their combined free memory
    is real capacity for a single model — the one case where summing across
    machines is correct."""

    async def healthy(self):
        return True

    async def fake_poll_once(self):
        snap = ClusterSnapshot(
            ts=datetime.now(UTC), nodes=[node("solo"), node("a"), node("b")]
        )
        self._latest = snap
        return snap

    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.healthy", healthy)
    monkeypatch.setattr("spark_dash_backend.poller.LivePoller.poll_once", fake_poll_once)

    app = create_app(
        Settings(
            spark_nodes="solo=10.0.0.1,pair/a=10.0.0.2,pair/b=10.0.0.3",
            prometheus_targets_dir=tmp_path,
            static_dir=tmp_path / "nostatic",
        )
    )
    with TestClient(app) as c:
        body = c.get("/api/cluster/summary").json()

    groups = {g["key"]: g for g in body["groups"]}
    assert set(groups) == {"solo", "pair"}
    assert groups["solo"]["standalone"] is True
    assert groups["pair"]["standalone"] is False
    assert sorted(groups["pair"]["nodes"]) == ["a", "b"]

    # Each node has 100GB free; the pair pools to 200GB, the solo node has 100.
    assert groups["pair"]["memory_free_bytes"] == 200_000_000_000
    assert groups["solo"]["memory_free_bytes"] == 100_000_000_000

    # The pair can host a model the standalone node could not.
    assert body["largest_free_block_bytes"] == 200_000_000_000
    assert body["largest_free_block_group"] == "pair"


def test_models_flattens_across_runtimes(client):
    rows = client.get("/api/models").json()["models"]
    names = {(r["model"], r["state"]) for r in rows}
    assert ("qwen36-35b", "active") in names
    # Sleeping models appear too — a warm cache is operationally distinct from
    # a cold start, and a boolean loaded/not would hide that.
    assert ("cydonia-24b", "sleeping") in names
    assert ("llama-3.3-70b", "active") in names


def test_models_includes_router_label(client):
    rows = client.get("/api/models").json()["models"]
    llama = [r for r in rows if r["runtime"] == "llama.cpp"]
    assert all(r["router"] == "192.168.50.61:8001" for r in llama)


def test_history_rejects_unknown_metric(client):
    """Callers pick from a fixed menu; a frontend bug can't become an
    arbitrary expensive PromQL query."""
    resp = client.get("/api/history", params={"metric": "'; DROP TABLE"})
    assert resp.status_code == 400
    assert "unknown metric" in resp.json()["detail"]


def test_history_requires_metric(client):
    assert client.get("/api/history").status_code == 422


def test_history_rejects_absurd_window(client):
    assert client.get("/api/history", params={"metric": "gpu_utilization",
                                              "minutes": 999999}).status_code == 422


def test_health_flags_the_down_node(client):
    """The fixture has gx10-2 down, so 1-of-2 reachable must read as degraded.
    Rounding a partial outage up to "ok" is how a dead node goes unnoticed."""
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["nodes_configured"] == 2
    assert body["nodes_up"] == 1
    assert any("1 of 2" in p for p in body["problems"])
    # Prometheus is fine here; only the node count is the problem.
    assert body["prometheus"] == "ok"


def test_health_degraded_when_prometheus_unreachable(tmp_path, monkeypatch):
    """Running but blind must not pass a naive uptime check."""

    async def unhealthy(self):
        return False

    async def fake_poll_once(self):
        snap = ClusterSnapshot(ts=datetime.now(UTC), nodes=[node("gx10-1")])
        self._latest = snap
        return snap

    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.healthy", unhealthy)
    monkeypatch.setattr("spark_dash_backend.poller.LivePoller.poll_once", fake_poll_once)

    app = create_app(
        Settings(
            spark_nodes="gx10-1=192.168.50.61",
            prometheus_targets_dir=tmp_path,
            static_dir=tmp_path / "nostatic",
        )
    )
    with TestClient(app) as c:
        body = c.get("/health").json()

    assert body["status"] == "degraded"
    assert "prometheus unreachable" in body["problems"]


def test_health_degraded_when_inventory_empty(tmp_path, monkeypatch):
    """No SPARK_NODES and no target file: the dashboard has nothing to show,
    and should say so rather than looking healthy-but-empty."""

    async def healthy(self):
        return True

    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.healthy", healthy)

    app = create_app(
        Settings(
            spark_nodes="",
            agent_targets_file=tmp_path / "missing.yml",
            prometheus_targets_dir=tmp_path,
            static_dir=tmp_path / "nostatic",
        )
    )
    with TestClient(app) as c:
        body = c.get("/health").json()

    assert body["status"] == "degraded"
    assert "inventory empty" in body["problems"]


def test_websocket_streams_snapshots(client):
    """The live path: a connecting client gets a frame without waiting a
    full poll interval."""
    with client.websocket_connect("/ws/live") as ws:
        payload = ws.receive_json()
    assert "nodes" in payload
    assert "ts" in payload


def test_startup_renders_prometheus_targets(tmp_path, monkeypatch):
    """The point of SPARK_NODES: Prometheus's scrape targets are derived from
    the same list the live view polls, so the two can't disagree."""

    async def healthy(self):
        return True

    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.healthy", healthy)

    app = create_app(
        Settings(
            spark_nodes="gx10-1=192.168.50.61,gx10-2=192.168.50.62",
            prometheus_targets_dir=tmp_path,
            static_dir=tmp_path / "nostatic",
        )
    )
    with TestClient(app):
        pass

    agents = (tmp_path / "agents.yml").read_text()
    exporters = (tmp_path / "node-exporters.yml").read_text()

    assert "192.168.50.61:9500" in agents
    assert "192.168.50.62:9500" in agents
    assert "192.168.50.61:9100" in exporters
    assert "node: gx10-2" in agents


class TestHealthActuallyChecksNodes:
    """Regression: /health reported "ok" with nodes_up null, having never
    contacted a node. The live poller only runs while a dashboard is open, so
    a monitoring system would have been watching a green light that meant
    nothing — the exact blind-but-green state this endpoint exists to catch.
    """

    def _app(self, tmp_path, monkeypatch, *, nodes_up: int, configured: str):
        async def healthy(self):
            return True

        polled = {"count": 0}

        async def fake_poll_once(self):
            polled["count"] += 1
            snap = ClusterSnapshot(
                ts=datetime.now(UTC),
                nodes=[node(f"n{i}", up=i < nodes_up) for i in range(len(configured.split(",")))],
            )
            self._latest = snap
            return snap

        monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.healthy", healthy)
        monkeypatch.setattr("spark_dash_backend.poller.LivePoller.poll_once", fake_poll_once)

        app = create_app(
            Settings(
                spark_nodes=configured,
                prometheus_targets_dir=tmp_path,
                static_dir=tmp_path / "nostatic",
            )
        )
        return app, polled

    def test_polls_when_it_has_no_snapshot(self, tmp_path, monkeypatch):
        app, polled = self._app(
            tmp_path, monkeypatch, nodes_up=1, configured="n0=10.0.0.1"
        )
        with TestClient(app) as c:
            body = c.get("/health").json()

        assert polled["count"] >= 1, "health must contact nodes, not trust an empty cache"
        assert body["nodes_up"] == 1
        # Every node is reachable, so nodes aren't the problem here. Status is
        # degraded only because Alertmanager isn't stubbed in this fixture —
        # which is itself correct: an unreachable Alertmanager means nothing
        # would notify you of anything.
        assert "no nodes reachable" not in body["problems"]
        assert body["problems"] == ["alertmanager unreachable"]

    def test_all_nodes_down_is_degraded(self, tmp_path, monkeypatch):
        app, _ = self._app(tmp_path, monkeypatch, nodes_up=0, configured="n0=10.0.0.1")
        with TestClient(app) as c:
            body = c.get("/health").json()

        assert body["status"] == "degraded"
        assert "no nodes reachable" in body["problems"]

    def test_partial_outage_is_degraded(self, tmp_path, monkeypatch):
        """On a 3-node cluster, losing one node is exactly what this should
        surface — not round up to healthy."""
        app, _ = self._app(
            tmp_path, monkeypatch, nodes_up=2, configured="n0=10.0.0.1,n1=10.0.0.2,n2=10.0.0.3"
        )
        with TestClient(app) as c:
            body = c.get("/health").json()

        assert body["status"] == "degraded"
        assert any("1 of 3" in p for p in body["problems"])

    def test_poll_failure_is_reported_not_raised(self, tmp_path, monkeypatch):
        async def healthy(self):
            return True

        async def boom(self):
            raise RuntimeError("network gone")

        monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.healthy", healthy)
        monkeypatch.setattr("spark_dash_backend.poller.LivePoller.poll_once", boom)

        app = create_app(
            Settings(
                spark_nodes="n0=10.0.0.1",
                prometheus_targets_dir=tmp_path,
                static_dir=tmp_path / "nostatic",
            )
        )
        with TestClient(app) as c:
            resp = c.get("/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert "could not reach any node to check" in body["problems"]
