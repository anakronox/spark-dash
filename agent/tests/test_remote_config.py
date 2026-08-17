"""Central runtime config, and the precedence that makes migration safe.

The rule is: central wins WHERE CENTRAL HAS AN OPINION.

  node is in cluster.yml   -> central config, env ignored
  node is absent from it   -> fall back to env
  backend unreachable      -> last known config, else env

That middle case is the one worth testing hardest. Deploying this agent before
adding a node to cluster.yml would otherwise take its model reporting dark the
moment it restarts — a bad way to discover an ordering mistake.
"""

import httpx
import pytest
from spark_dash_agent.remote_config import RemoteConfig, RuntimeConfig

CONFIGURED = {
    "node": "sparky",
    "configured": True,
    "runtimes": {
        "llama_routers": [
            {"url": "http://192.168.50.61:8001", "scrape_metrics": True},
            {"url": "http://192.168.50.61:8108", "scrape_metrics": False},
        ],
        "vllm": ["http://192.168.50.61:8120/metrics"],
    },
}

ABSENT = {"node": "newcomer", "configured": False, "runtimes": {}}


@pytest.fixture
def patched(monkeypatch):
    def _install(payload, *, status=200, boom=False):
        def handler(request):
            if boom:
                raise httpx.ConnectError("refused")
            return httpx.Response(status, json=payload)

        transport = httpx.MockTransport(handler)
        real_client = httpx.Client

        def factory(*args, **kwargs):
            kwargs.setdefault("transport", transport)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "Client", factory)

    return _install


class TestPrecedence:
    def test_configured_node_gets_central_runtimes(self, patched):
        patched(CONFIGURED)
        rc = RemoteConfig("http://backend:8080", "sparky")
        cfg = rc.current(now=1000.0)
        assert cfg is not None
        assert cfg.llama_routers == [
            "http://192.168.50.61:8001",
            "http://192.168.50.61:8108",
        ]
        assert cfg.vllm == ["http://192.168.50.61:8120/metrics"]

    def test_scrape_metrics_becomes_the_allowlist(self, patched):
        """Only routers flagged in central config may be scraped per-model —
        the same opt-in that stops an idle model being pinned in memory."""
        patched(CONFIGURED)
        cfg = RemoteConfig("http://backend:8080", "sparky").current(now=1000.0)
        assert cfg.metrics_allowlist == ["http://192.168.50.61:8001"]

    def test_absent_node_returns_none_so_env_wins(self, patched):
        """The migration-safety case. A node not yet in cluster.yml must fall
        back to its environment rather than go dark, or deploying the agent
        before editing the file would silently stop its model reporting."""
        patched(ABSENT)
        assert RemoteConfig("http://backend:8080", "newcomer").current(now=1000.0) is None

    def test_configured_but_empty_still_overrides_env(self, patched):
        """A node listed with no runtimes genuinely serves nothing. That is an
        opinion, and it must beat leftover env vars — otherwise removing a
        router centrally would never take effect on a node that still has the
        old value in its .env."""
        patched({"node": "n", "configured": True, "runtimes": {}})
        cfg = RemoteConfig("http://backend:8080", "n").current(now=1000.0)
        assert cfg == RuntimeConfig()

    def test_no_backend_url_means_central_is_off(self):
        """How a deployment that has not migrated keeps working."""
        rc = RemoteConfig("", "sparky")
        assert rc.enabled is False
        assert rc.current(now=1000.0) is None


class TestResilience:
    def test_unreachable_backend_keeps_the_last_good_config(self, patched, monkeypatch):
        patched(CONFIGURED)
        rc = RemoteConfig("http://backend:8080", "sparky", ttl_s=10)
        first = rc.current(now=1000.0)
        assert first is not None

        # Backend goes away; the TTL expires.
        patched(None, boom=True)
        later = rc.current(now=2000.0)
        assert later == first, "a transient outage must not blank a node's routers"

    def test_http_error_keeps_the_last_good_config(self, patched):
        patched(CONFIGURED)
        rc = RemoteConfig("http://backend:8080", "sparky", ttl_s=10)
        first = rc.current(now=1000.0)
        patched({"detail": "boom"}, status=500)
        assert rc.current(now=2000.0) == first

    def test_fetches_once_within_the_ttl(self, monkeypatch):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(200, json=CONFIGURED)

        transport = httpx.MockTransport(handler)
        real = httpx.Client
        monkeypatch.setattr(
            httpx, "Client", lambda *a, **k: real(*a, **{**k, "transport": transport})
        )

        rc = RemoteConfig("http://backend:8080", "sparky", ttl_s=60)
        rc.current(now=1000.0)
        rc.current(now=1010.0)
        rc.current(now=1050.0)
        assert calls["n"] == 1, "must not hit the backend on every snapshot"

        rc.current(now=1061.0)
        assert calls["n"] == 2, "and must refresh once the TTL has passed"


def _install(monkeypatch, handler):
    """Same shape as the `patched` fixture, but with a caller-supplied handler
    so a test can change the backend's behaviour mid-run."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)


def test_status_reports_the_last_SUCCESS_not_the_last_attempt(monkeypatch):
    """`_fetched_at` advances on failure too, so a dead backend is retried on
    the TTL rather than on every tick. Reporting THAT as the fetch time would
    tell a reader their edit had arrived when the last thing that happened was
    a timeout."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=CONFIGURED)
        raise httpx.ConnectError("backend down")

    _install(monkeypatch, handler)
    rc = RemoteConfig("http://backend", "spark2", ttl_s=0.0)

    rc.current(100.0)
    source, first_ok = rc.status(100.0)
    assert source == "central"
    assert first_ok == 100.0

    rc.current(200.0)
    _, after_failure = rc.status(200.0)
    assert after_failure == first_ok, "a failed fetch moved the 'last answered' time"


def test_status_distinguishes_never_answered_from_never_asking(monkeypatch):
    """A node asking and getting silence runs on env by ACCIDENT. A node never
    pointed at a backend runs on env by DESIGN. Different faults, and only one
    of them wants investigating."""

    def dead(request):
        raise httpx.ConnectError("no route")

    _install(monkeypatch, dead)
    rc = RemoteConfig("http://backend", "spark2", ttl_s=0.0)
    rc.current(10.0)
    assert rc.status(10.0) == ("unreachable", None)

    assert RemoteConfig("", "", ttl_s=0.0).status(0.0) == ("env", None)


def test_status_says_env_when_the_node_is_absent_from_cluster_yml(monkeypatch, patched):
    """Central answered, and its answer was "I don't manage this node"."""
    patched(ABSENT)
    rc = RemoteConfig("http://backend", "newcomer", ttl_s=0.0)
    rc.current(50.0)
    source, at = rc.status(50.0)
    assert source == "env"
    assert at == 50.0
