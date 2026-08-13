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
    targets = tmp_path / "agents.yml"
    targets.write_text(INVENTORY)

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

    app = create_app(Settings(agent_targets_file=targets, static_dir=tmp_path / "nostatic"))
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
    assert body["memory_free_bytes"] == 100_000_000_000


def test_cluster_summary_reports_free_capacity(client):
    """The number that answers 'can I load another model'."""
    body = client.get("/api/cluster/summary").json()
    assert body["memory_total_bytes"] - body["memory_used_bytes"] == body["memory_free_bytes"]


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


def test_health_ok_when_everything_reachable(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["nodes_configured"] == 2
    assert body["problems"] == []


def test_health_degraded_when_prometheus_unreachable(tmp_path, monkeypatch):
    """Running but blind must not pass a naive uptime check."""
    targets = tmp_path / "agents.yml"
    targets.write_text(INVENTORY)

    async def unhealthy(self):
        return False

    async def fake_poll_once(self):
        snap = ClusterSnapshot(ts=datetime.now(UTC), nodes=[node("gx10-1")])
        self._latest = snap
        return snap

    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.healthy", unhealthy)
    monkeypatch.setattr("spark_dash_backend.poller.LivePoller.poll_once", fake_poll_once)

    app = create_app(Settings(agent_targets_file=targets, static_dir=tmp_path / "nostatic"))
    with TestClient(app) as c:
        body = c.get("/health").json()

    assert body["status"] == "degraded"
    assert "prometheus unreachable" in body["problems"]


def test_health_degraded_when_inventory_empty(tmp_path, monkeypatch):
    empty = tmp_path / "agents.yml"
    empty.write_text("[]")

    async def healthy(self):
        return True

    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.healthy", healthy)

    app = create_app(Settings(agent_targets_file=empty, static_dir=tmp_path / "nostatic"))
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
