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
