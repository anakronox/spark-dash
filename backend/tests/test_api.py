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
    EngineMetrics,
    GpuMetrics,
    HealthState,
    LlamaRouterMetrics,
    MemoryMetrics,
    ModelState,
    NodeSnapshot,
    RouterModel,
    Runtimes,
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
                    generation_tokens_per_sec=12.4,
                )
            ],
            vllm=[EngineMetrics(
                model="llama-3.3-70b",
                tokens_per_sec=88.5,
                generation_tokens_per_sec=31.0,
                prompt_tokens_per_sec=57.5,
            )],
            sglang=[EngineMetrics(model="deepseek-v3", server="192.168.50.61:30000")],
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


def test_cluster_summary_reports_decode_not_decode_plus_prefill(client):
    """The headline number is DECODE throughput.

    Adding prefill in made it unreadable: measured on a live cluster
    2026-08-21, the combined figure hit 47,672 tok/s while the model generated
    48. A large prompt landing inside one poll window really is that fast to
    ingest, and it is not what anyone reads a throughput stat to learn.
    """
    body = client.get("/api/cluster/summary").json()
    assert body["nodes_total"] == 2
    assert body["nodes_up"] == 1
    # 12.4 (llama.cpp) + 31.0 (vLLM) on the one live node. NOT 129.7, which is
    # what the same fixture sums to with prefill folded in.
    assert body["tokens_per_second"] == pytest.approx(43.4)


def test_a_prefill_burst_does_not_reach_the_headline():
    """The live defect, reproduced with the numbers it was measured at.

    On `danflashes` 2026-08-21, `rate(vllm:prompt_tokens_total[5m])` peaked at
    3375/s while generation peaked at 47.9/s, and single-poll samples of the
    combined figure reached 47,672. A reader glancing at Throughput during
    prefill saw a number three orders of magnitude off what the model was
    producing.
    """
    from spark_dash_common.models import EngineMetrics, NodeSnapshot, Runtimes

    snap = NodeSnapshot(
        node_id="sparketa",
        ts=datetime.now(UTC),
        up=True,
        runtimes=Runtimes(vllm=[EngineMetrics(
            model="deepseek-v4-flash-0731",
            generation_tokens_per_sec=47.9,
            prompt_tokens_per_sec=47624.1,
            tokens_per_sec=47672.0,
        )]),
    )
    assert snap.total_generation_tokens_per_sec == pytest.approx(47.9)
    # The combined series is still reported — it is what history is written
    # against — it is simply not what the headline reads.
    assert snap.total_tokens_per_sec == pytest.approx(47672.0)


def test_largest_free_block_is_per_cluster_not_a_total(client):
    """The fixture's two nodes are ungrouped, so each is its own group. The
    answer to "can I load this" is the best single group can offer — summing
    them would describe capacity that doesn't exist, since a model can't span
    machines that aren't clustered."""
    body = client.get("/api/cluster/summary").json()
    # Only gx10-1 is up, with 100GB free. gx10-2 is down and contributes none.
    assert body["largest_free_block_bytes"] == 100_000_000_000
    assert body["largest_free_block_cluster"] == "gx10-1"


def test_unclustered_nodes_are_each_their_own_cluster(client):
    body = client.get("/api/cluster/summary").json()
    groups = {g["key"]: g for g in body["clusters"]}
    assert set(groups) == {"gx10-1", "gx10-2"}
    assert all(g["standalone"] for g in groups.values())
    assert all(g["cluster"] is None for g in groups.values())


def test_clustered_nodes_pool_their_memory(tmp_path, monkeypatch):
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
            spark_nodes="solo=10.0.0.1,alpha/a=10.0.0.2,alpha/b=10.0.0.3",
            prometheus_targets_dir=tmp_path,
            static_dir=tmp_path / "nostatic",
        )
    )
    with TestClient(app) as c:
        body = c.get("/api/cluster/summary").json()

    groups = {g["key"]: g for g in body["clusters"]}
    assert set(groups) == {"solo", "alpha"}
    assert groups["solo"]["standalone"] is True
    assert groups["alpha"]["standalone"] is False
    assert sorted(groups["alpha"]["nodes"]) == ["a", "b"]

    # Each node has 100GB free; the pair pools to 200GB, the solo node has 100.
    assert groups["alpha"]["memory_free_bytes"] == 200_000_000_000
    assert groups["solo"]["memory_free_bytes"] == 100_000_000_000

    # The pair can host a model the standalone node could not.
    assert body["largest_free_block_bytes"] == 200_000_000_000
    assert body["largest_free_block_cluster"] == "alpha"


def test_models_flattens_across_runtimes(client):
    rows = client.get("/api/models").json()["models"]
    names = {(r["model"], r["state"]) for r in rows}
    assert ("qwen36-35b", "active") in names
    # Sleeping models appear too — a warm cache is operationally distinct from
    # a cold start, and a boolean loaded/not would hide that.
    assert ("cydonia-24b", "sleeping") in names
    assert ("llama-3.3-70b", "active") in names


def test_models_names_the_engine_each_row_came_from(client):
    """Every engine's instances become rows, labelled with the engine — the
    table is node x RUNTIME x model, and pooling two engines under one name
    would make "what is serving this" unanswerable from it."""
    rows = client.get("/api/models").json()["models"]
    by_model = {r["model"]: r["runtime"] for r in rows}
    assert by_model["llama-3.3-70b"] == "vllm"
    assert by_model["deepseek-v3"] == "sglang"


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


def test_history_refuses_node_filter_on_aggregations(client):
    """Appending `{node="x"}` to `sum by (node) (...)` is not valid PromQL.

    Attempting it anyway reached Prometheus and came back a 503, which reads
    like an outage rather than like the caller having asked for something that
    cannot be expressed.
    """
    resp = client.get(
        "/api/history", params={"metric": "tokens_per_second", "node": "sparky"}
    )
    assert resp.status_code == 400
    assert "cannot be filtered by node" in resp.json()["detail"]


def test_history_allows_node_filter_on_bare_selectors(client):
    """The simple metrics still take the filter — this is a narrowing, not a
    removal.

    Asserted as "not rejected" rather than as 200: there is no real Prometheus
    behind the test client, so the request gets as far as querying and then
    fails. What matters here is that it was not turned away at the door.
    """
    resp = client.get(
        "/api/history", params={"metric": "gpu_utilization", "node": "sparky"}
    )
    assert resp.status_code != 400


def test_every_history_query_is_valid_promql_shape():
    """Every rate-based query must carry the window placeholder, and nothing
    else may — a query that keeps a literal `{window}` would reach Prometheus
    as a syntax error, and one that hardcodes a window silently ignores the
    step."""
    from spark_dash_backend.prometheus import HISTORY_QUERIES, rate_window

    for key, expr in HISTORY_QUERIES.items():
        filled = expr.replace("{window}", rate_window("60s"))
        assert "{window}" not in filled, key
        if "rate(" in expr:
            assert "{window}" in expr, f"{key} uses rate() with a fixed window"


def test_rate_window_scales_with_step_and_has_a_floor():
    from spark_dash_backend.prometheus import rate_window

    assert rate_window("60s") == "240s"
    assert rate_window("600s") == "2400s"
    # A window shorter than a couple of scrapes yields nothing at all.
    assert rate_window("5s") == "60s"
    assert rate_window("2m") == "480s"
    # Unparseable input must not raise in a request path. It lands on the same
    # window a default 60s step would, which is the step the endpoint defaults
    # to — a fallback that disagreed with the default would be its own bug.
    assert rate_window("") == "240s"


def test_node_exporter_queries_exclude_the_monitoring_vm():
    """node_exporter also runs on the monitoring VM under its own job. Without
    the filter it would appear in every chart as a node the cluster does not
    contain."""
    from spark_dash_backend.prometheus import HISTORY_QUERIES

    for key in ("psi_cpu_some", "psi_io_some", "cpu_clock", "disk_busy"):
        assert 'job="node-exporter"' in HISTORY_QUERIES[key], key


def test_health_flags_a_configured_endpoint_that_is_not_answering():
    """A node can be reachable and still report nothing about a router because
    the port in cluster.yml is wrong. Every measured part is healthy, so the
    node looks fine — which is why it has to be said out loud."""
    from datetime import UTC, datetime

    from spark_dash_backend.app import _unreachable_endpoints
    from spark_dash_common.models import (
        ClusterSnapshot,
        EngineMetrics,
        LlamaRouterMetrics,
        NodeSnapshot,
        Runtimes,
    )

    snap = ClusterSnapshot(
        ts=datetime.now(UTC),
        nodes=[
            NodeSnapshot(
                node_id="spark2",
                ts=datetime.now(UTC),
                up=True,
                runtimes=Runtimes(
                    llama_cpp=[
                        LlamaRouterMetrics(endpoint="http://h:8001", reachable=True),
                        LlamaRouterMetrics(endpoint="http://h:8002", reachable=False),
                    ],
                    vllm=[EngineMetrics(model="h:8000", server="h:8000", reachable=False)],
                ),
            )
        ],
    )
    assert _unreachable_endpoints(snap) == [
        "spark2 · llama.cpp · http://h:8002",
        "spark2 · vllm · h:8000",
    ]


def test_unreachable_endpoints_ignores_a_node_that_is_down():
    """On a down node every endpoint is unreachable. Listing each one buries
    the single fact that matters under a list of its consequences."""
    from datetime import UTC, datetime

    from spark_dash_backend.app import _unreachable_endpoints
    from spark_dash_common.models import (
        ClusterSnapshot,
        LlamaRouterMetrics,
        NodeSnapshot,
        Runtimes,
    )

    snap = ClusterSnapshot(
        ts=datetime.now(UTC),
        nodes=[
            NodeSnapshot(
                node_id="spark3",
                ts=datetime.now(UTC),
                up=False,
                runtimes=Runtimes(
                    llama_cpp=[LlamaRouterMetrics(endpoint="http://h:8001", reachable=False)]
                ),
            )
        ],
    )
    assert _unreachable_endpoints(snap) == []


def test_authority_ignores_scheme_path_and_trailing_slash():
    """Prometheus names an instance host:port; the config holds a URL.
    Comparing whole strings would make retire a silent no-op — the button
    appears to work and the target comes straight back."""
    from spark_dash_backend.cluster import authority as _authority

    assert _authority("http://192.168.50.61:8120/metrics") == "192.168.50.61:8120"
    assert _authority("http://192.168.50.61:8120/") == "192.168.50.61:8120"
    assert _authority("192.168.50.61:8120") == "192.168.50.61:8120"


def test_retire_refuses_infrastructure_targets(client):
    """The line is environmental vs scraped. Hardware still exists, so being
    able to delete those alerts would let someone permanently blind the
    dashboard to a real failure."""
    resp = client.delete(
        "/api/targets/absent",
        params={"job": "node-exporter", "instance": "192.168.50.61:9100"},
    )
    assert resp.status_code == 400
    assert "only inference targets" in resp.json()["detail"]


def test_retire_accepts_every_engine_job(client):
    """The job name IS the runtime name, so the gate is membership rather than
    a second list to keep in step. A 404 here means it got past the gate and
    found no matching endpoint — which is the point being tested."""
    resp = client.delete(
        "/api/targets/absent", params={"job": "sglang", "instance": "10.0.0.1:30000"}
    )
    assert resp.status_code == 404
    assert "sglang" in resp.json()["detail"]


def test_retire_reports_not_found_rather_than_silently_succeeding(client):
    """A retire that matched nothing must say so. Reporting success would leave
    the reader believing a dead target was gone when it is still configured."""
    resp = client.delete(
        "/api/targets/absent", params={"job": "vllm", "instance": "10.0.0.1:9999"}
    )
    assert resp.status_code == 404


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


def test_health_reports_the_backend_own_build(client):
    """AgentBuildSkew compares nodes against EACH OTHER, so it cannot see a
    backend and an agent that have drifted apart — and with a single node it
    cannot fire at all. `/health` naming its own build is the only thing that
    makes that visible, and it is how a stale agent was spotted on 2026-08-16."""
    body = client.get("/health").json()
    assert "backend_version" in body
    assert isinstance(body["backend_version"], str)
    assert body["backend_version"]


class TestDataFreshness:
    """Reachable and recording are different things.

    A clock step on 2026-08-16 left Prometheus answering queries perfectly
    while rejecting every incoming sample. Alertmanager was fine, so the banner
    rendered a confident "nothing firing" over half an hour of no data. The API
    has to be able to say which of those it means.
    """

    def test_alerts_reports_data_freshness(self, client):
        body = client.get("/api/alerts").json()
        assert "data_stale" in body
        assert "data_age_s" in body

    def test_unknown_freshness_is_treated_as_stale(self, client, monkeypatch):
        """Not knowing is not the same as being fine, and must not render as
        reassurance."""
        import spark_dash_backend.app as app_mod  # noqa: F401

        async def unknown(self):
            return None

        monkeypatch.setattr(
            "spark_dash_backend.prometheus.PrometheusClient.data_age_s", unknown
        )
        assert client.get("/api/alerts").json()["data_stale"] is True

    def test_fresh_data_is_not_stale(self, client, monkeypatch):
        async def fresh(self):
            return 3.0

        monkeypatch.setattr(
            "spark_dash_backend.prometheus.PrometheusClient.data_age_s", fresh
        )
        body = client.get("/api/alerts").json()
        assert body["data_stale"] is False
        assert body["data_age_s"] == 3.0

    def test_old_data_is_stale(self, client, monkeypatch):
        async def old(self):
            return 600.0

        monkeypatch.setattr(
            "spark_dash_backend.prometheus.PrometheusClient.data_age_s", old
        )
        assert client.get("/api/alerts").json()["data_stale"] is True


# ------------------------------------------------------------ maintenance
#
# The Alertmanager client is replaced with an in-memory one: what these
# tests check is the HTTP contract and the wiring — scope resolution through
# the inventory, the peers silence, the way out — not Alertmanager itself.


class InMemoryAlertmanager:
    def __init__(self):
        self.store: dict[str, dict] = {}
        self.n = 0
        self.expired: list[str] = []

    async def reachable(self):
        return True

    async def firing(self):
        return []

    async def silences(self):
        return [s for s in self.store.values() if s["status"]["state"] == "active"]

    async def silenced(self):
        return []

    async def create_silence(self, matchers, *, hours, comment, author="spark-dash"):
        from datetime import timedelta

        self.n += 1
        sid = f"sil-{self.n}"
        now = datetime.now(UTC)
        self.store[sid] = {
            "id": sid,
            "createdBy": author,
            "comment": comment,
            "matchers": matchers,
            "startsAt": now.isoformat(),
            "endsAt": (now + timedelta(hours=hours)).isoformat(),
            "status": {"state": "active"},
        }
        return sid

    async def expire_silence(self, sid):
        self.expired.append(sid)
        self.store[sid]["status"]["state"] = "expired"


@pytest.fixture
def maint(tmp_path, monkeypatch):
    am = InMemoryAlertmanager()
    monkeypatch.setattr("spark_dash_backend.app.AlertmanagerClient", lambda *a, **k: am)

    async def fake_poll_once(self):
        snap = ClusterSnapshot(ts=datetime.now(UTC), nodes=[node("gx10-1"), node("gx10-2")])
        self._stamp(snap)
        self._latest = snap
        return snap

    async def fake_healthy(self):
        return True

    async def fake_age(self):
        return 5.0

    monkeypatch.setattr("spark_dash_backend.poller.LivePoller.poll_once", fake_poll_once)
    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.healthy", fake_healthy)
    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.data_age_s", fake_age)

    app = create_app(
        Settings(
            # A pair, so node scope has peers to mute.
            spark_nodes="danflashes/gx10-1=192.168.50.61,danflashes/gx10-2=192.168.50.62",
            prometheus_targets_dir=tmp_path,
            static_dir=tmp_path / "nostatic",
        )
    )
    with TestClient(app) as c:
        yield c, am


def test_maintenance_starts_a_window_and_lists_it(maint):
    client, am = maint
    resp = client.post(
        "/api/maintenance",
        json={"scope": "node", "name": "gx10-1", "hours": 2, "reason": "trying Qwen3"},
    )
    assert resp.status_code == 200, resp.text
    w = resp.json()["window"]
    assert w["scope"] == "node"
    assert w["name"] == "gx10-1"
    assert w["nodes"] == ["gx10-1"]
    assert w["reason"] == "trying Qwen3"
    # Node in a cluster: the node silence AND the peers silence.
    assert len(w["silence_ids"]) == 2
    assert {s["createdBy"] for s in am.store.values()} == {"spark-dash/maintenance"}

    listed = client.get("/api/maintenance").json()
    assert listed["available"] is True
    assert [x["id"] for x in listed["windows"]] == [w["id"]]


def test_maintenance_rides_the_alerts_payload(maint):
    client, _ = maint
    client.post("/api/maintenance", json={"scope": "cluster", "name": "danflashes"})
    body = client.get("/api/alerts").json()
    assert len(body["maintenance"]) == 1
    assert sorted(body["maintenance"][0]["nodes"]) == ["gx10-1", "gx10-2"]


def test_maintenance_default_is_four_hours_and_capped_like_a_silence(maint):
    client, am = maint
    client.post("/api/maintenance", json={"scope": "node", "name": "gx10-1"})
    from datetime import datetime as dt

    s = next(iter(am.store.values()))
    hours = (dt.fromisoformat(s["endsAt"]) - dt.fromisoformat(s["startsAt"])).total_seconds() / 3600
    assert round(hours, 3) == 4.0

    too_long = client.post(
        "/api/maintenance", json={"scope": "node", "name": "gx10-1", "hours": 48}
    )
    assert too_long.status_code == 422


def test_maintenance_unknown_scope_is_404_not_an_empty_mute(maint):
    client, am = maint
    resp = client.post("/api/maintenance", json={"scope": "node", "name": "nope"})
    assert resp.status_code == 404
    assert am.store == {}, "nothing must be created for a scope that matches nothing"


def test_maintenance_ends_every_silence_in_the_window(maint):
    client, am = maint
    w = client.post("/api/maintenance", json={"scope": "node", "name": "gx10-1"}).json()["window"]
    resp = client.delete(f"/api/maintenance/{w['id']}")
    assert resp.status_code == 200
    assert sorted(am.expired) == sorted(w["silence_ids"])
    assert client.get("/api/maintenance").json()["windows"] == []
    assert client.delete(f"/api/maintenance/{w['id']}").status_code == 404


def test_maintenance_is_stamped_onto_the_live_snapshot(maint):
    client, _ = maint
    client.post("/api/maintenance", json={"scope": "node", "name": "gx10-2", "reason": "swap"})
    # A REST call first, so the poller holds a (stubbed) snapshot; the socket
    # then hands that frame over immediately rather than polling real hosts.
    client.get("/api/nodes")
    with client.websocket_connect("/ws/live") as ws:
        frame = ws.receive_json()
    by_id = {n["node_id"]: n for n in frame["nodes"]}
    assert by_id["gx10-1"]["maintenance"] is None
    assert by_id["gx10-2"]["maintenance"]["reason"] == "swap"
    assert by_id["gx10-2"]["health"] == "good", "the mark never rewrites health"


def test_metrics_expose_one_series_per_covered_node(maint):
    client, _ = maint
    client.post("/api/maintenance", json={"scope": "cluster", "name": "danflashes"})
    text = client.get("/metrics").text
    assert "# TYPE sparkdash_maintenance gauge" in text
    assert 'sparkdash_maintenance{node="gx10-1",scope="cluster",name="danflashes"' in text
    assert 'sparkdash_maintenance{node="gx10-2",scope="cluster",name="danflashes"' in text


def test_silences_carry_their_window(maint):
    client, _ = maint
    client.post("/api/maintenance", json={"scope": "node", "name": "gx10-1", "reason": "r"})
    silences = client.get("/api/alerts/silences").json()["silences"]
    assert len(silences) == 2
    assert {s["maintenance"]["peers"] for s in silences} == {False, True}
    assert {s["maintenance"]["name"] for s in silences} == {"gx10-1"}
