"""End-to-end checks on the agent's HTTP surface, with collection stubbed.

Uses a fake builder so these run anywhere — the real collectors need NVML and a
GB10, which CI and a dev laptop don't have.
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry, generate_latest
from spark_dash_agent.app import SnapshotCache, create_app
from spark_dash_agent.config import Settings
from spark_dash_agent.exporter import SnapshotMetricsCollector
from spark_dash_common.models import (
    ClockState,
    CpuMetrics,
    GpuMetrics,
    HealthState,
    LlamaRouterMetrics,
    MemoryMetrics,
    ModelState,
    NodeSnapshot,
    ProcessInfo,
    PsiMetrics,
    PsiState,
    RouterModel,
    Runtimes,
    VllmMetrics,
)


def make_snapshot(**overrides) -> NodeSnapshot:
    defaults = dict(
        node_id="gx10-1",
        ts=datetime.now(UTC),
        up=True,
        health=HealthState.GOOD,
        health_reasons=[],
        gpu=GpuMetrics(
            util_pct=87.0,
            temp_c=72.0,
            power_w=94.0,
            clock_mhz=2380.0,
            clock_state=ClockState.PASS,
        ),
        memory=MemoryMetrics(
            total_bytes=128_000_000_000,
            available_bytes=37_000_000_000,
            used_bytes=91_000_000_000,
            swap_used_bytes=0,
            unified=True,
        ),
        psi=PsiMetrics(some_avg10=0.4, state=PsiState.LOW),
        cpu=CpuMetrics(util_pct=22.0, temp_c=58.0, load_avg_1m=3.2, active_cores=20),
        processes=[
            ProcessInfo(
                pid=4412,
                name="llama-server",
                gpu_mem_bytes=42_000_000_000,
                runtime="llama.cpp",
                model="qwen3-32b",
            )
        ],
        runtimes=Runtimes(
            llama_cpp=[
                LlamaRouterMetrics(
                    endpoint="http://router-a:8080",
                    name="router-a:8080",
                    models=[
                        RouterModel(
                            name="qwen3-32b",
                            state=ModelState.ACTIVE,
                            raw_status="loaded",
                            tokens_per_sec=41.2,
                            kv_cache_pct=55.0,
                        ),
                        RouterModel(
                            name="cydonia-24b", state=ModelState.SLEEPING, raw_status="sleeping"
                        ),
                    ],
                    max_instances=3,
                    autoload=True,
                    tokens_per_sec=41.2,
                )
            ],
            vllm=[VllmMetrics(model="llama-3.3-70b", tokens_per_sec=88.5, kv_cache_pct=63.0)],
        ),
    )
    defaults.update(overrides)
    return NodeSnapshot(**defaults)


class FakeBuilder:
    def __init__(self, snapshot: NodeSnapshot | None = None):
        self.snapshot = snapshot or make_snapshot()
        self.build_count = 0
        self.node_id = self.snapshot.node_id

    def build(self) -> NodeSnapshot:
        self.build_count += 1
        return self.snapshot


@pytest.fixture
def client(monkeypatch):
    builder = FakeBuilder()
    monkeypatch.setattr("spark_dash_agent.app.SnapshotBuilder", lambda settings: builder)
    app = create_app(Settings(node_id="gx10-1"))
    with TestClient(app) as c:
        c.builder = builder  # type: ignore[attr-defined]
        yield c


def test_snapshot_endpoint_returns_typed_payload(client):
    resp = client.get("/snapshot")
    assert resp.status_code == 200
    body = resp.json()
    assert body["node_id"] == "gx10-1"
    assert body["gpu"]["util_pct"] == 87.0
    assert body["memory"]["unified"] is True
    assert body["processes"][0]["name"] == "llama-server"


def test_health_reports_ok_when_no_collector_failed(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["failed_collectors"] == []


def test_health_reports_degraded_when_a_collector_failed(monkeypatch):
    """Still serving, but incomplete — a watcher should say so rather than
    showing a bare green tick."""
    builder = FakeBuilder(make_snapshot(errors={"gpu": "NVMLError: driver not loaded"}))
    monkeypatch.setattr("spark_dash_agent.app.SnapshotBuilder", lambda settings: builder)
    with TestClient(create_app(Settings())) as c:
        body = c.get("/health").json()
    assert body["status"] == "degraded"
    assert body["failed_collectors"] == ["gpu"]


def test_metrics_endpoint_exposes_prometheus_text(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    text = resp.text
    assert 'sparkdash_gpu_utilization_percent{node="gx10-1"} 87.0' in text
    assert 'sparkdash_memory_used_bytes{node="gx10-1"} 9.1e+10' in text
    assert "sparkdash_memory_unified" in text


def test_cache_collapses_concurrent_reads(client):
    """Collection touches NVML and does HTTP round-trips; it must not run once
    per request when Prometheus and the live poll arrive together."""
    before = client.builder.build_count
    for _ in range(5):
        client.get("/snapshot")
        client.get("/metrics")
    assert client.builder.build_count - before <= 1


def test_cache_refreshes_after_ttl():
    builder = FakeBuilder()
    cache = SnapshotCache(builder, ttl_s=0.0)
    cache.get()
    cache.get()
    assert builder.build_count == 2


# --- exporter shape -------------------------------------------------------


def render(snapshot: NodeSnapshot) -> str:
    registry = CollectorRegistry()
    registry.register(SnapshotMetricsCollector(lambda: snapshot))
    return generate_latest(registry).decode()


def test_states_render_one_series_per_state():
    """Alerting rules match on the state label rather than decoding an enum."""
    text = render(make_snapshot())
    assert 'sparkdash_gpu_clock_state{node="gx10-1",state="PASS"} 1.0' in text
    assert 'sparkdash_gpu_clock_state{node="gx10-1",state="THROTTLED"} 0.0' in text
    assert 'sparkdash_node_health{node="gx10-1",state="good"} 1.0' in text


def test_build_is_exported_as_an_info_gauge():
    """A git sha can't be a gauge value, so it rides as a label with a constant
    1 — the same shape as rdma_port_info. Under :latest nothing in config
    records which build is deployed, so this is the only historical answer to
    "what was running when"."""
    text = render(make_snapshot(agent_version="1cccead"))
    assert 'sparkdash_agent_build_info{build="1cccead",node="gx10-1"} 1.0' in text


def test_build_info_reports_unknown_rather_than_vanishing():
    """Running from source has no commit to name. The series must still exist,
    or a node built outside publish-images.sh would silently drop out of the
    skew check instead of standing out in it."""
    text = render(make_snapshot(agent_version="unknown"))
    assert 'sparkdash_agent_build_info{build="unknown",node="gx10-1"} 1.0' in text


def test_endpoint_reachable_exports_healthy_endpoints_too():
    """The series must EXIST for a working endpoint, not only a broken one.

    If it appeared only on failure, `absent()` could not tell "not configured"
    from "not answering" — two conditions needing different alerts. 1 =
    answering, 0 = configured and silent.
    """
    text = render(
        make_snapshot(
            runtimes=Runtimes(
                llama_cpp=[
                    LlamaRouterMetrics(endpoint="http://h:8001", name="prod"),
                    LlamaRouterMetrics(
                        endpoint="http://h:8002", name="lab", reachable=False
                    ),
                ],
                vllm=[VllmMetrics(model="h:8000", server="h:8000", reachable=False)],
            )
        )
    )
    assert (
        'sparkdash_endpoint_reachable{endpoint="http://h:8001",node="gx10-1",'
        'runtime="llama.cpp"} 1.0' in text
    )
    assert (
        'sparkdash_endpoint_reachable{endpoint="http://h:8002",node="gx10-1",'
        'runtime="llama.cpp"} 0.0' in text
    )
    assert (
        'sparkdash_endpoint_reachable{endpoint="h:8000",node="gx10-1",'
        'runtime="vllm"} 0.0' in text
    )


def test_endpoint_reachable_absent_when_nothing_is_configured():
    """A node serving no inference emits no series at all, so `absent()` keeps
    meaning "nothing is configured here"."""
    text = render(make_snapshot(runtimes=Runtimes()))
    assert "sparkdash_endpoint_reachable" not in text


def test_processes_are_exported_aggregated_never_per_pid():
    """PIDs churn on every model swap, so a pid label would grow cardinality
    without bound and never reuse a series. Aggregating by workload identity
    keeps it bounded by configuration instead — but the pid itself, and the raw
    process name, must never reach Prometheus."""
    text = render(make_snapshot())
    assert "4412" not in text
    assert "pid=" not in text
    # The process *name* is per-process detail; the model is the stable identity.
    assert 'name="llama-server"' not in text
    assert (
        'sparkdash_gpu_process_memory_bytes{model="qwen3-32b",node="gx10-1",'
        'runtime="llama.cpp",server=""} 4.2e+10' in text
    )


def test_process_memory_sums_within_a_workload():
    """Two children of the same model must add up rather than overwrite."""
    text = render(
        make_snapshot(
            processes=[
                ProcessInfo(pid=1, name="llama-server", gpu_mem_bytes=1_000, runtime="llama.cpp"),
                ProcessInfo(pid=2, name="llama-server", gpu_mem_bytes=2_500, runtime="llama.cpp"),
            ]
        )
    )
    labels = 'model="",node="gx10-1",runtime="llama.cpp",server=""'
    assert f"sparkdash_gpu_process_memory_bytes{{{labels}}} 3500.0" in text
    assert f"sparkdash_gpu_process_count{{{labels}}} 2.0" in text


def test_unlabeled_process_still_reports_its_memory():
    """An unrecognized process eating the unified pool is exactly what you want
    to see — it must not be dropped for lacking a runtime."""
    text = render(
        make_snapshot(processes=[ProcessInfo(pid=9, name="mystery", gpu_mem_bytes=5_000)])
    )
    labels = 'model="",node="gx10-1",runtime="",server=""'
    assert f"sparkdash_gpu_process_memory_bytes{{{labels}}} 5000.0" in text


def test_missing_sections_are_omitted_not_zeroed():
    """A failed collector must not look like a real zero reading."""
    text = render(make_snapshot(gpu=None, memory=None, psi=None, cpu=None))
    assert "sparkdash_gpu_utilization_percent" not in text
    assert "sparkdash_memory_used_bytes" not in text
    assert "sparkdash_node_up" in text


def test_collector_errors_are_exported():
    text = render(make_snapshot(errors={"psi": "boom"}))
    assert 'sparkdash_collector_errors{collector="psi",node="gx10-1"} 1.0' in text


def test_runtime_metrics_are_labeled_by_model():
    text = render(make_snapshot())
    assert (
        "sparkdash_llama_model_tokens_per_second"
        '{model="qwen3-32b",node="gx10-1",router="router-a:8080"} 41.2'
    ) in text
    assert 'sparkdash_vllm_tokens_per_second{model="llama-3.3-70b",node="gx10-1"} 88.5' in text
    assert 'sparkdash_llama_models_known{node="gx10-1",router="router-a:8080"} 2.0' in text
    assert 'sparkdash_llama_models_active{node="gx10-1",router="router-a:8080"} 1.0' in text
    assert 'sparkdash_llama_models_sleeping{node="gx10-1",router="router-a:8080"} 1.0' in text
    assert 'sparkdash_llama_router_max_instances{node="gx10-1",router="router-a:8080"} 3.0' in text


def test_model_state_renders_one_series_per_state():
    text = render(make_snapshot())
    assert (
        "sparkdash_llama_model_state"
        '{model="qwen3-32b",node="gx10-1",router="router-a:8080",state="active"} 1.0'
    ) in text
    assert (
        "sparkdash_llama_model_state"
        '{model="cydonia-24b",node="gx10-1",router="router-a:8080",state="sleeping"} 1.0'
    ) in text


def test_sleeping_models_get_no_throughput_series():
    """A sleeping model has no weights; emitting 0 throughput would be
    indistinguishable from a loaded-but-idle model."""
    text = render(make_snapshot())
    assert 'sparkdash_llama_model_tokens_per_second{model="cydonia-24b"' not in text
    assert (
        "sparkdash_llama_model_tokens_per_second"
        '{model="qwen3-32b",node="gx10-1",router="router-a:8080"} 41.2'
    ) in text


def test_router_label_distinguishes_multiple_routers():
    """A node runs several router containers, and the same model name can be
    registered with more than one — the router label is what separates them."""
    snap = make_snapshot(
        runtimes=Runtimes(
            llama_cpp=[
                LlamaRouterMetrics(
                    endpoint="http://a:8080",
                    name="a:8080",
                    models=[
                        RouterModel(name="shared", state=ModelState.ACTIVE, tokens_per_sec=10.0)
                    ],
                ),
                LlamaRouterMetrics(
                    endpoint="http://b:8081",
                    name="b:8081",
                    models=[
                        RouterModel(name="shared", state=ModelState.ACTIVE, tokens_per_sec=20.0)
                    ],
                ),
            ]
        )
    )
    text = render(snap)
    assert 'router="a:8080"' in text
    assert 'router="b:8081"' in text
    assert (
        'sparkdash_llama_model_tokens_per_second{model="shared",node="gx10-1",router="a:8080"} 10.0'
    ) in text


def test_unreachable_router_is_reported_as_down():
    """One router down must be visible, not silently absent."""
    snap = make_snapshot(
        runtimes=Runtimes(
            llama_cpp=[
                LlamaRouterMetrics(endpoint="http://dead:8080", name="dead:8080", reachable=False)
            ]
        )
    )
    text = render(snap)
    assert 'sparkdash_llama_router_up{node="gx10-1",router="dead:8080"} 0.0' in text


def test_metrics_content_type_matches_the_generator(client):
    """Regression: the endpoint advertised OpenMetrics while serving the
    plain-text format. Prometheus honours the header, parses strictly, and
    rejects the body for a missing trailing "# EOF" — so every scrape returned
    200 OK while Prometheus stored nothing and marked the target down.
    """
    resp = client.get("/metrics")
    content_type = resp.headers["content-type"]
    body = resp.text

    assert content_type.startswith("text/plain")
    assert "openmetrics" not in content_type

    # Plain-text format has no EOF marker; OpenMetrics requires one. Their
    # presence/absence is what makes the two mutually exclusive.
    assert not body.rstrip().endswith("# EOF")


def test_metrics_body_parses_as_prometheus_text(client):
    """Parse the served bytes with the same parser Prometheus uses, so a
    malformed exposition can't pass unnoticed."""
    from prometheus_client.parser import text_string_to_metric_families

    families = list(text_string_to_metric_families(client.get("/metrics").text))
    names = {f.name for f in families}
    assert "sparkdash_gpu_utilization_percent" in names
