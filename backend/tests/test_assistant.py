"""Ask the dashboard (roadmap AY): its settings, its key, its context."""

from __future__ import annotations

import asyncio
import os
import stat
from types import SimpleNamespace as NS

import pytest
import yaml
from fastapi.testclient import TestClient

from spark_dash_backend import assistant
from spark_dash_backend.app import create_app
from spark_dash_backend.cluster import ClusterConfigError
from spark_dash_backend.config import Settings
from spark_dash_backend.prometheus import Series

NODES = "nodes:\n- id: sparky\n  host: 127.0.0.1\n  agent_port: 9\n"


@pytest.fixture
def api(tmp_path, monkeypatch):
    async def fake_healthy(self):
        return True

    async def fake_query(self, expr):
        return []

    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.healthy", fake_healthy)
    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.query", fake_query)
    cluster = tmp_path / "cluster" / "cluster.yml"
    cluster.parent.mkdir()
    cluster.write_text(NODES)
    (tmp_path / "targets").mkdir()
    app = create_app(Settings(cluster_config=cluster, prometheus_targets_dir=tmp_path / "targets",
                              static_dir=tmp_path / "nostatic", agent_timeout_s=0.5))
    with TestClient(app) as client:
        client.cluster = cluster
        yield client


class TestUrl:
    @pytest.mark.parametrize("raw, want", [
        ("http://gw:4000/v1", "http://gw:4000/v1"),
        ("http://gw:4000", "http://gw:4000/v1"),
        ("gw:4000", "http://gw:4000/v1"),
        ("https://gw/openai/v1/", "https://gw/openai/v1"),
    ])
    def test_an_openai_compatible_base(self, raw, want):
        assert assistant.normalize_url(raw) == want

    @pytest.mark.parametrize("raw", ["", "ftp://gw", "http://user:pw@gw:4000/v1", "http://gw:4000/v1?key=x"])
    def test_refused(self, raw):
        with pytest.raises(ClusterConfigError):
            assistant.normalize_url(raw)


class TestSettings:
    def test_the_key_never_goes_to_the_browser_or_the_cluster_file(self, api):
        body = api.put("/api/assistant/config", json={
            "url": "http://gw.invalid:4000", "model": "danflashes/GLM", "key": "sk-supersecret1234"}).json()
        assert body["configured"] and body["key_set"] and body["key_hint"] == "…1234"
        assert "supersecret" not in str(body)
        assert "supersecret" not in api.cluster.read_text()
        assert yaml.safe_load(api.cluster.read_text())["assistant"] == {
            "url": "http://gw.invalid:4000/v1", "model": "danflashes/GLM", "enabled": True}
        key = assistant.key_path(api.cluster)
        assert key.read_text().strip() == "sk-supersecret1234"
        assert stat.S_IMODE(os.stat(key).st_mode) == 0o600

    def test_saving_again_without_a_key_keeps_it(self, api):
        api.put("/api/assistant/config", json={"url": "gw.invalid:4000", "model": "m", "key": "sk-keep-this-one"})
        api.put("/api/assistant/config", json={"url": "gw.invalid:4000", "model": "other"})
        assert assistant.read_key(api.cluster) == "sk-keep-this-one"

    def test_the_nodes_survive_a_save(self, api):
        api.put("/api/assistant/config", json={"url": "gw.invalid:4000", "model": "m"})
        assert yaml.safe_load(api.cluster.read_text())["nodes"][0]["id"] == "sparky"

    def test_removing_it_removes_the_key_too(self, api):
        api.put("/api/assistant/config", json={"url": "gw.invalid:4000", "model": "m", "key": "sk-12345678"})
        body = api.delete("/api/assistant/config").json()
        assert (body["configured"], body["key_set"]) == (False, False)
        assert "assistant" not in yaml.safe_load(api.cluster.read_text())

    def test_asking_before_it_is_set_up_says_so(self, api):
        r = api.post("/api/assistant/ask", json={"question": "why?"})
        assert r.status_code == 409 and "Settings" in r.json()["detail"]


def test_the_context_names_what_moved_and_when():
    """The model gets the window's alerts, events and series, per node."""

    class Prom:
        async def query(self, expr):
            return [Series(labels={"alertname": "GpuHot", "severity": "warning", "node": "sparky"},
                           points=[(0, 1)])]

        async def query_range(self, expr, start, end, step):
            if "temperature" not in expr:
                return []
            return [Series(labels={"node": "sparky"}, points=[(start, 50.0), (start + 600, 81.0), (end, 60.0)])]

    node = NS(model_dump=lambda mode: {
        "node_id": "sparky", "up": True, "cluster": None, "health": "warning", "health_reasons": ["GPU hot"],
        "gpu": {"util_pct": 96, "temp_c": 80, "power_w": 58, "clock_state": "PASS"},
        "memory": {"available_bytes": 100 * 2**30, "total_bytes": 121.7 * 2**30, "swap_used_bytes": 0},
        "cpu": {"util_pct": 5, "temp_c": 70}, "psi": {"state": "low", "some_avg10": 0, "full_avg10": 0},
        "processes": [], "runtimes": {"llama_cpp": [], "tensorfold": []},
    })
    episode = NS(alertname="GpuHot", severity="warning", node="sparky", started_at=1e9, ended_at=1e9 + 300,
                 ongoing=False, fired=True)
    event = NS(ts=1e9, kind="cold-start", label="qwen loaded", node="sparky")
    text, counts = asyncio.run(assistant.gather_context(
        Prom(), [node], minutes=60, episodes=[episode], annotations=[event], latency=None))
    assert "sparky: up, health warning (GPU hot)" in text
    assert "ALERTS FIRING NOW: GpuHot (warning, sparky)" in text
    assert "GpuHot (warning, sparky): 2001-09-09 01:46 to 2001-09-09 01:51" in text
    assert "cold-start: qwen loaded (sparky)" in text
    assert "gpu_temperature: sparky 50.0/63.7/81.0 (peak" in text
    assert counts["alert_episodes"] == 1 and counts["events"] == 1


def test_the_assistant_names_itself_to_the_endpoint():
    """Otherwise its requests read as httpx from the monitoring host on the
    Clients card."""
    from spark_dash_backend.clients import classify

    h = assistant._headers("sk-1234")
    assert h["User-Agent"].startswith("spark-dash-assistant/")
    assert classify(h["User-Agent"]) == {"name": "spark-dash assistant", "kind": "harness"}
    assert "Authorization" not in assistant._headers(None)


def test_the_context_says_who_called_and_when_tokens_began():
    class Prom:
        async def query(self, expr):
            return []

        async def query_range(self, expr, start, end, step):
            return []

    clients = {"tokens_since": 1e9, "rows": [{
        "harness": {"name": "Hermes Agent", "kind": "harness", "detail": None},
        "client": {"name": "hermes"}, "model": "danflashes/GLM", "listed": True,
        "engine": {"node": "sparketa", "runtime": "tensorfold", "server": "x"},
        "requests": 227, "per_min": 4.2, "failed": 0, "tokens_in": 3_200_000, "tokens_out": 9000,
    }]}
    text, counts = asyncio.run(assistant.gather_context(
        Prom(), [], minutes=60, episodes=[], annotations=[], latency=None, clients=clients))
    assert "CLIENTS through the gateway in window, busiest first (token counts only since 2001-09-09 01:46):" in text
    assert "Hermes Agent from hermes: danflashes/GLM on sparketa tensorfold: 227 requests, 4.2/min, 0 failed, tokens 3200000 in / 9000 out" in text
    assert counts["clients"] == 1
