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
    LoadedModel,
    MemoryMetrics,
    NodeSnapshot,
    ProcessInfo,
    PsiMetrics,
    PsiState,
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
            llama_cpp=LlamaRouterMetrics(
                loaded_models=[
                    LoadedModel(name="qwen3-32b", tokens_per_sec=41.2, kv_cache_pct=55.0)
                ],
                known_model_count=4,
                tokens_per_sec=41.2,
            ),
            vllm=[VllmMetrics(model="llama-3.3-70b", tokens_per_sec=88.5, kv_cache_pct=63.0)],
        ),
    )
    defaults.update(overrides)
    return NodeSnapshot(**defaults)


class FakeBuilder:
    def __init__(self, snapshot: NodeSnapshot | None = None):
        self.snapshot = snapshot or make_snapshot()
        self.build_count = 0

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


def test_process_list_is_not_exported_to_prometheus():
    """PIDs churn; a pid label would grow series cardinality without bound for
    data nobody queries historically. It's live-view-only, served via JSON."""
    text = render(make_snapshot())
    assert "4412" not in text
    assert "process" not in text.lower()


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
    assert 'sparkdash_llama_model_tokens_per_second{model="qwen3-32b",node="gx10-1"} 41.2' in text
    assert 'sparkdash_vllm_tokens_per_second{model="llama-3.3-70b",node="gx10-1"} 88.5' in text
    assert 'sparkdash_llama_models_known{node="gx10-1"} 4.0' in text
