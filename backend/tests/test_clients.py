"""Client stats from a LiteLLM gateway (roadmap AM).

AM3 so far: the gateway becomes a Prometheus target only when it is configured
AND switched on, a typo in its block never stops a scrape that was working,
and /health says which of those states it is in.
"""

from __future__ import annotations

import asyncio

import pytest
import yaml
from fastapi.testclient import TestClient
from spark_dash_backend.app import create_app
from spark_dash_backend.clients import gateway_health
from spark_dash_backend.cluster import GatewayConfig
from spark_dash_backend.config import Settings
from spark_dash_backend.inventory import Inventory, write_prometheus_targets
from spark_dash_backend.prometheus import PrometheusError, Series

# Nodes on a port nothing listens on, so /health's poll fails fast rather
# than reaching for real hardware.
NODES = "nodes:\n- id: sparky\n  host: 127.0.0.1\n  agent_port: 9\n"


def gateway_block(url="http://litellm.invalid:4000", enabled=True):
    return f"gateway:\n  litellm:\n    url: {url}\n    enabled: {str(enabled).lower()}\n"


def targets(out):
    return yaml.safe_load((out / "litellm.yml").read_text())


class TestScrapeTarget:
    def _sync(self, tmp_path, text):
        cluster = tmp_path / "cluster.yml"
        cluster.write_text(text)
        out = tmp_path / "targets"
        out.mkdir(exist_ok=True)
        Inventory(cluster_config=cluster, prometheus_targets_dir=out).sync_prometheus_targets()
        return out

    def test_configured_and_on_is_one_target(self, tmp_path):
        out = self._sync(tmp_path, gateway_block() + NODES)
        assert targets(out) == [{"targets": ["litellm.invalid:4000"]}]

    def test_configured_but_off_is_no_target(self, tmp_path):
        out = self._sync(tmp_path, gateway_block(enabled=False) + NODES)
        assert targets(out) == []

    def test_no_gateway_still_writes_an_empty_file(self, tmp_path):
        """The job is always declared, so the file must always exist."""
        out = self._sync(tmp_path, NODES)
        assert targets(out) == []

    def test_https_is_carried_per_target(self, tmp_path):
        out = self._sync(tmp_path, gateway_block(url="https://litellm.invalid") + NODES)
        assert targets(out) == [
            {"targets": ["litellm.invalid"], "labels": {"__scheme__": "https"}}
        ]

    def test_a_typo_keeps_the_last_good_target(self, tmp_path):
        """Stopping the scrape over a hand edit would lose history for nothing."""
        out = self._sync(tmp_path, gateway_block() + NODES)
        out = self._sync(tmp_path, "gateway:\n  litellm:\n    url: not-a-url\n" + NODES)
        assert targets(out) == [{"targets": ["litellm.invalid:4000"]}]

    def test_turning_it_off_empties_the_target(self, tmp_path):
        out = self._sync(tmp_path, gateway_block() + NODES)
        out = self._sync(tmp_path, gateway_block(enabled=False) + NODES)
        assert targets(out) == []

    def test_spark_nodes_deployments_get_an_empty_file(self, tmp_path):
        """No cluster file, so no gateway: written empty, never left missing."""
        from spark_dash_backend.inventory import parse_nodes_env

        write_prometheus_targets(parse_nodes_env("sparky=127.0.0.1"), tmp_path, source="env")
        assert targets(tmp_path) == []

    def test_the_file_names_its_source(self, tmp_path):
        out = self._sync(tmp_path, gateway_block() + NODES)
        assert "GENERATED FILE" in (out / "litellm.yml").read_text()


class FakeProm:
    def __init__(self, up=None, fail=False):
        self.up, self.fail, self.asked = up, fail, []

    async def query(self, expr):
        self.asked.append(expr)
        if self.fail:
            raise PrometheusError("down")
        if self.up is None:
            return []
        return [Series(labels={"job": "litellm"}, points=[(0.0, self.up)])]


class TestGatewayHealth:
    GW = GatewayConfig(url="http://litellm.invalid:4000")

    def _run(self, gateway, error=None, prom=None):
        return asyncio.run(gateway_health(gateway, error, prom or FakeProm()))

    def test_not_configured(self):
        assert self._run(None) == "not configured"

    def test_off_asks_prometheus_nothing(self):
        prom = FakeProm(up=1)
        assert self._run(GatewayConfig(url="http://g.invalid", enabled=False), prom=prom) == "off"
        assert prom.asked == []

    def test_ok_when_the_scrape_is_up(self):
        assert self._run(self.GW, prom=FakeProm(up=1)) == "ok"

    @pytest.mark.parametrize("up", [0, None])
    def test_not_scraped_when_down_or_not_picked_up_yet(self, up):
        assert self._run(self.GW, prom=FakeProm(up=up)) == "not scraped"

    def test_unknown_when_prometheus_is_down(self):
        assert self._run(self.GW, prom=FakeProm(fail=True)) == "unknown"

    def test_invalid_names_the_problem(self):
        assert self._run(None, error="gateway.litellm.url is missing").startswith("invalid: ")


def test_health_carries_the_gateway_and_keeps_it_out_of_problems(tmp_path, monkeypatch):
    """Not a `problems` entry: the dashboard is not blind without a gateway."""

    async def fake_healthy(self):
        return True

    async def fake_query(self, expr):
        assert expr == 'up{job="litellm"}'
        return [Series(labels={"job": "litellm"}, points=[(0.0, 0.0)])]

    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.healthy", fake_healthy)
    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.query", fake_query)
    cluster = tmp_path / "cluster" / "cluster.yml"
    cluster.parent.mkdir()
    cluster.write_text(gateway_block() + NODES)
    (tmp_path / "targets").mkdir()
    app = create_app(
        Settings(
            cluster_config=cluster,
            prometheus_targets_dir=tmp_path / "targets",
            static_dir=tmp_path / "nostatic",
            agent_timeout_s=0.5,
        )
    )
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["litellm"] == "not scraped"
    assert not any("litellm" in p for p in body["problems"])
    assert targets(tmp_path / "targets") == [{"targets": ["litellm.invalid:4000"]}]


# ------------------------------------------------------------------ AM4a


METRICS_TEXT = (
    "# HELP litellm_proxy_total_requests_metric_total Total requests\n"
    "# TYPE litellm_proxy_total_requests_metric_total counter\n"
)


def gateway_transport(seen, *, alive=200, metrics=200, body=METRICS_TEXT, redirect_to=None):
    """A fake LiteLLM that records every URL it is asked for. /metrics
    redirects to /metrics/, as the real one does."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/health/liveliness":
            return httpx.Response(alive, text="I'm alive!")
        if request.url.path == "/metrics":
            target = redirect_to or f"{request.url.scheme}://{request.url.netloc.decode()}/metrics/"
            return httpx.Response(307, headers={"location": target})
        if request.url.path == "/metrics/":
            return httpx.Response(metrics, text=body)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class TestProbe:
    """The Test button. THE GUARD: every outcome is checked against the exact
    list of paths requested, and bare /health is never one of them. On
    LiteLLM, /health sends a real completion to every model, which on an
    autoloading llama.cpp router loads them all."""

    def _probe(self, monkeypatch, url="http://litellm.invalid:4000/v1", **kw):
        from spark_dash_backend import clients

        seen: list[str] = []
        monkeypatch.setattr(clients, "probe_transport", gateway_transport(seen, **kw))
        result = asyncio.run(clients.probe_gateway(url))
        assert "/health" not in seen
        return result, seen

    def test_a_readable_gateway(self, monkeypatch):
        result, seen = self._probe(monkeypatch)
        assert seen == ["/health/liveliness", "/metrics", "/metrics/"]
        assert result["url"] == "http://litellm.invalid:4000"
        assert (result["reachable"], result["metrics"]) == (True, "ok")

    def test_metrics_behind_a_key_names_the_setting(self, monkeypatch):
        result, _ = self._probe(monkeypatch, metrics=401)
        assert result["metrics"] == "needs a key"
        assert "require_auth_for_metrics_endpoint" in result["detail"]

    def test_metrics_without_litellm_series(self, monkeypatch):
        result, _ = self._probe(monkeypatch, body="# TYPE python_info gauge\n")
        assert result["metrics"] == "missing"
        assert "callback" in result["detail"]

    def test_not_a_litellm_proxy_stops_at_liveliness(self, monkeypatch):
        result, seen = self._probe(monkeypatch, alive=404)
        assert seen == ["/health/liveliness"]
        assert result["reachable"] is False

    def test_a_redirect_off_the_host_is_not_followed_as_success(self, monkeypatch):
        result, _ = self._probe(monkeypatch, redirect_to="http://elsewhere.invalid/metrics/")
        assert result["metrics"] == "error"
        assert "elsewhere.invalid" in result["detail"]

    def test_no_answer(self, monkeypatch):
        import httpx
        from spark_dash_backend import clients

        def refuse(request):
            raise httpx.ConnectError("refused", request=request)

        monkeypatch.setattr(clients, "probe_transport", httpx.MockTransport(refuse))
        result = asyncio.run(clients.probe_gateway("http://litellm.invalid:4000"))
        assert result["reachable"] is False
        assert "No answer" in result["detail"]

    def test_a_bad_address_is_refused_before_any_request(self, monkeypatch):
        from spark_dash_backend.cluster import ClusterConfigError

        with pytest.raises(ClusterConfigError):
            self._probe(monkeypatch, url="http://litellm.invalid:4000/health")


@pytest.fixture
def api(tmp_path, monkeypatch):
    """The app over a real cluster file, with Prometheus faked: `up` for the
    litellm job reads 1."""

    async def fake_healthy(self):
        return True

    async def fake_query(self, expr):
        return [Series(labels={"job": "litellm"}, points=[(0.0, 1.0)])]

    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.healthy", fake_healthy)
    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.query", fake_query)
    cluster = tmp_path / "cluster" / "cluster.yml"
    cluster.parent.mkdir()
    cluster.write_text(NODES)
    (tmp_path / "targets").mkdir()
    app = create_app(
        Settings(
            cluster_config=cluster,
            prometheus_targets_dir=tmp_path / "targets",
            static_dir=tmp_path / "nostatic",
            agent_timeout_s=0.5,
        )
    )
    with TestClient(app) as client:
        client.cluster = cluster
        client.targets = tmp_path / "targets"
        yield client


class TestRoutes:
    def test_status_before_anything_is_set(self, api):
        body = api.get("/api/clients/status").json()
        assert body == {
            "capability": False,
            "enabled": False,
            "configured": False,
            "url": None,
            "error": None,
            "cluster_file": True,
            "state": "not configured",
        }

    def test_setting_the_pasted_client_url_starts_the_scrape(self, api):
        body = api.put("/api/clients/config", json={"url": "http://litellm.invalid:4000/v1"}).json()
        assert body["url"] == "http://litellm.invalid:4000"
        assert (body["capability"], body["enabled"], body["state"]) == (True, True, "ok")
        assert targets(api.targets) == [{"targets": ["litellm.invalid:4000"]}]
        # And the node list came through the write untouched.
        assert yaml.safe_load(api.cluster.read_text())["nodes"][0]["id"] == "sparky"

    def test_the_toggle_stops_and_restarts_the_scrape(self, api):
        api.put("/api/clients/config", json={"url": "http://litellm.invalid:4000"})
        off = api.post("/api/clients/enabled", json={"enabled": False}).json()
        assert (off["capability"], off["enabled"], off["state"]) == (True, False, "off")
        assert targets(api.targets) == []
        on = api.post("/api/clients/enabled", json={"enabled": True}).json()
        assert on["enabled"] is True
        assert targets(api.targets) == [{"targets": ["litellm.invalid:4000"]}]

    def test_changing_the_address_keeps_the_toggle(self, api):
        api.put("/api/clients/config", json={"url": "http://litellm.invalid:4000"})
        api.post("/api/clients/enabled", json={"enabled": False})
        body = api.put("/api/clients/config", json={"url": "http://other.invalid:4000"}).json()
        assert (body["url"], body["enabled"]) == ("http://other.invalid:4000", False)

    def test_removing_the_address(self, api):
        api.put("/api/clients/config", json={"url": "http://litellm.invalid:4000"})
        body = api.put("/api/clients/config", json={"url": None}).json()
        assert body["capability"] is False
        assert "gateway" not in yaml.safe_load(api.cluster.read_text())
        assert targets(api.targets) == []

    def test_the_toggle_needs_an_address(self, api):
        response = api.post("/api/clients/enabled", json={"enabled": True})
        assert response.status_code == 409
        assert "address" in response.json()["detail"]

    def test_a_bad_address_is_a_400_with_the_reason(self, api):
        response = api.put("/api/clients/config", json={"url": "http://litellm.invalid/metrics"})
        assert response.status_code == 400
        assert "not a path" in response.json()["detail"]

    def test_a_broken_block_is_reported_and_blocks_the_toggle(self, api):
        api.cluster.write_text("gateway:\n  litellm:\n    url: nope\n" + NODES)
        status = api.get("/api/clients/status").json()
        assert status["error"] and status["state"].startswith("invalid: ")
        assert api.post("/api/clients/enabled", json={"enabled": True}).status_code == 409
        # Saving a good address repairs it.
        good = api.put("/api/clients/config", json={"url": "http://litellm.invalid:4000"}).json()
        assert good["error"] is None

    def test_test_route_probes_without_saving(self, api, monkeypatch):
        from spark_dash_backend import clients

        seen: list[str] = []
        monkeypatch.setattr(clients, "probe_transport", gateway_transport(seen))
        body = api.post("/api/clients/test", json={"url": "http://litellm.invalid:4000/v1"}).json()
        assert body["metrics"] == "ok"
        assert "/health" not in seen
        assert "gateway" not in yaml.safe_load(api.cluster.read_text())


def test_no_cluster_file_is_a_409_that_says_why(tmp_path, monkeypatch):
    async def fake_healthy(self):
        return True

    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.healthy", fake_healthy)
    (tmp_path / "targets").mkdir()
    app = create_app(
        Settings(
            cluster_config=tmp_path / "cluster" / "cluster.yml",
            spark_nodes="sparky=127.0.0.1",
            prometheus_targets_dir=tmp_path / "targets",
            static_dir=tmp_path / "nostatic",
            agent_timeout_s=0.5,
        )
    )
    with TestClient(app) as api:
        assert api.get("/api/clients/status").json()["cluster_file"] is False
        response = api.put("/api/clients/config", json={"url": "http://litellm.invalid:4000"})
    assert response.status_code == 409
    assert "SPARK_NODES" in response.json()["detail"]
    assert not (tmp_path / "cluster" / "cluster.yml").exists()
