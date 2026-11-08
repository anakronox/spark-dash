"""llama.cpp router API keys on the agent side: the shared token sent to the
backend, the keys taken from its answer, and each router's key sent only to
that router. And never a key in a log line."""

from __future__ import annotations

import logging

import httpx
import pytest
from spark_dash_agent.collectors.llama_router import LlamaRouterCollector
from spark_dash_agent.remote_config import RemoteConfig

KEY = "k" * 64
TOKEN = "sda-shared-token-value"

CONFIG = {
    "node": "sparky",
    "configured": True,
    "runtimes": {
        "llama_routers": [
            {"url": "http://192.168.50.61:8001", "scrape_metrics": False},
            {"url": "http://192.168.50.61:8108", "scrape_metrics": False},
        ],
    },
}


@pytest.fixture
def backend(monkeypatch):
    """A fake backend recording the Authorization header it was sent, and
    answering with router keys only when the token matches, as the real one does."""
    seen: list[str | None] = []

    def handler(request):
        auth = request.headers.get("authorization")
        seen.append(auth)
        body = dict(CONFIG)
        if auth == f"Bearer {TOKEN}":
            body["router_keys"] = {
                "http://192.168.50.61:8108": KEY,
                "http://elsewhere:9999": "not-for-this-node",
            }
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    return seen


class TestRemoteConfig:
    def test_the_token_is_sent_and_keys_are_taken_for_this_nodes_routers_only(self, backend):
        cfg = RemoteConfig("http://backend:8080", "sparky", token=TOKEN).current(now=1000.0)
        assert backend == [f"Bearer {TOKEN}"]
        assert cfg is not None and cfg.router_keys == {"http://192.168.50.61:8108": KEY}

    def test_without_a_token_nothing_is_sent_and_there_are_no_keys(self, backend):
        cfg = RemoteConfig("http://backend:8080", "sparky").current(now=1000.0)
        assert backend == [None]
        assert cfg is not None and cfg.router_keys == {}

    def test_keys_never_reach_a_log_line_or_a_repr(self, backend, caplog):
        caplog.set_level(logging.DEBUG)
        cfg = RemoteConfig("http://backend:8080", "sparky", token=TOKEN).current(now=1000.0)
        assert KEY not in caplog.text and TOKEN not in caplog.text
        assert KEY not in repr(cfg)
        assert "http://192.168.50.61:8108" in caplog.text  # which router has a key: fine


class TestCollector:
    def test_each_router_gets_only_its_own_key(self):
        seen: dict[str, set[str | None]] = {}

        def handler(request):
            seen.setdefault(f"{request.url.host}:{request.url.port}", set()).add(
                request.headers.get("authorization")
            )
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": []})
            return httpx.Response(200, json={})

        LlamaRouterCollector(
            ["http://r:8001", "http://r:8108"],
            api_keys={"http://r:8108": KEY},
            transport=httpx.MockTransport(handler),
        ).collect()
        assert seen["r:8108"] == {f"Bearer {KEY}"}
        assert seen["r:8001"] == {None}

    def test_a_keyed_router_without_its_key_reads_unreachable(self):
        def handler(request):
            if request.headers.get("authorization") != f"Bearer {KEY}":
                return httpx.Response(401, json={"error": "Invalid API Key"})
            return httpx.Response(200, json={"data": []})

        transport = httpx.MockTransport(handler)
        without = LlamaRouterCollector(["http://r:8108"], transport=transport).collect()
        with_key = LlamaRouterCollector(
            ["http://r:8108"], api_keys={"http://r:8108": KEY}, transport=transport
        ).collect()
        assert without[0].reachable is False
        assert with_key[0].reachable is True
