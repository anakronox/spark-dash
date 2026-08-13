"""The critical property under test: we must never request
`/metrics?model=X` for a model that isn't already loaded.

Doing so triggers autoload and resets the idle-sleep timer
(ggml-org/llama.cpp#23096), which would make monitoring the thing that fills the
node's memory and prevents the router's LRU eviction from ever working.
"""

import httpx
import pytest
from spark_dash_agent.collectors.llama_router import (
    LlamaRouterCollector,
    RateTracker,
    parse_model_metrics,
)

METRICS_BODY = """# HELP llamacpp:prompt_tokens_total Prompt tokens
# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total 1000
# HELP llamacpp:tokens_predicted_total Predicted tokens
# TYPE llamacpp:tokens_predicted_total counter
llamacpp:tokens_predicted_total 5000
# HELP llamacpp:requests_processing Active requests
# TYPE llamacpp:requests_processing gauge
llamacpp:requests_processing 2
# HELP llamacpp:requests_deferred Queued requests
# TYPE llamacpp:requests_deferred gauge
llamacpp:requests_deferred 1
# HELP llamacpp:kv_cache_usage_ratio KV cache
# TYPE llamacpp:kv_cache_usage_ratio gauge
llamacpp:kv_cache_usage_ratio 0.63
"""


class RecordingRouter:
    """Fake router that records every path requested."""

    def __init__(self, models: list[dict]):
        self.models = models
        self.requested: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requested.append(str(request.url))
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": self.models})
            if request.url.path == "/metrics":
                return httpx.Response(200, text=METRICS_BODY)
            return httpx.Response(404)

        return httpx.MockTransport(handler)

    @property
    def metrics_requests(self) -> list[str]:
        return [u for u in self.requested if "/metrics" in u]


def test_never_scrapes_metrics_for_unloaded_models():
    """The whole point of the module. An unloaded model must not be touched."""
    router = RecordingRouter(
        [
            {"id": "loaded-model", "loaded": True},
            {"id": "sleeping-model", "loaded": False},
            {"id": "another-sleeper", "loaded": False},
        ]
    )
    collector = LlamaRouterCollector("http://router:8080", transport=router.transport())
    result = collector.collect()

    assert result is not None
    assert [m.name for m in result.loaded_models] == ["loaded-model"]
    assert len(router.metrics_requests) == 1
    assert "loaded-model" in router.metrics_requests[0]
    for url in router.metrics_requests:
        assert "sleeping-model" not in url
        assert "another-sleeper" not in url


def test_unknown_residency_shape_fails_safe():
    """An unrecognized /v1/models shape must mean 'not loaded', not 'probe it'.

    Key names vary across llama.cpp builds; guessing wrong here would wake every
    registered model.
    """
    router = RecordingRouter([{"id": "mystery-model", "some_unknown_field": 1}])
    collector = LlamaRouterCollector("http://router:8080", transport=router.transport())
    result = collector.collect()

    assert result is not None
    assert result.loaded_models == []
    assert result.known_model_count == 1
    assert router.metrics_requests == []


def test_reports_all_known_models_even_when_none_loaded():
    router = RecordingRouter(
        [{"id": "a", "loaded": False}, {"id": "b", "loaded": False}]
    )
    result = LlamaRouterCollector("http://r", transport=router.transport()).collect()
    assert result is not None
    assert result.known_model_count == 2
    assert result.loaded_models == []


@pytest.mark.parametrize(
    "entry,expected_loaded",
    [
        ({"id": "m", "loaded": True}, True),
        ({"id": "m", "is_loaded": True}, True),
        ({"id": "m", "resident": True}, True),
        ({"id": "m", "state": "loaded"}, True),
        ({"id": "m", "state": "ready"}, True),
        ({"id": "m", "status": "active"}, True),
        ({"id": "m", "state": "sleeping"}, False),
        ({"id": "m", "loaded": False}, False),
        ({"id": "m"}, False),
    ],
)
def test_residency_key_variants(entry, expected_loaded):
    """Tolerate the shapes different llama.cpp builds emit."""
    router = RecordingRouter([entry])
    result = LlamaRouterCollector("http://r", transport=router.transport()).collect()
    assert result is not None
    assert bool(result.loaded_models) is expected_loaded


def test_scrape_disabled_keeps_model_list_without_touching_metrics():
    """The escape hatch: still report what's loaded, fetch nothing."""
    router = RecordingRouter([{"id": "loaded-model", "loaded": True}])
    collector = LlamaRouterCollector(
        "http://r", transport=router.transport(), scrape_loaded_model_metrics=False
    )
    result = collector.collect()

    assert result is not None
    assert [m.name for m in result.loaded_models] == ["loaded-model"]
    assert router.metrics_requests == []


def test_returns_none_when_no_router_configured():
    """vLLM-only nodes run the same image with no router URL set."""
    assert LlamaRouterCollector(None).collect() is None


def test_returns_none_when_router_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    collector = LlamaRouterCollector("http://r", transport=httpx.MockTransport(handler))
    assert collector.collect() is None


def test_parses_model_metrics():
    collector = LlamaRouterCollector(
        "http://r", transport=RecordingRouter([{"id": "m", "loaded": True}]).transport()
    )
    result = collector.collect()
    assert result is not None
    model = result.loaded_models[0]
    assert model.requests_running == 2
    assert model.requests_waiting == 1
    assert model.kv_cache_pct == pytest.approx(63.0)


def test_parse_model_metrics_flattens_families():
    values = parse_model_metrics(METRICS_BODY)
    assert values["llamacpp:tokens_predicted_total"] == 5000.0
    assert values["llamacpp:kv_cache_usage_ratio"] == 0.63


class TestRateTracker:
    def test_first_sample_has_no_rate(self):
        """Nothing to compare against yet — must not invent a spike."""
        assert RateTracker().rate("k", 100.0, now=0.0) == 0.0

    def test_computes_per_second_rate(self):
        tracker = RateTracker()
        tracker.rate("k", 100.0, now=0.0)
        assert tracker.rate("k", 200.0, now=10.0) == pytest.approx(10.0)

    def test_counter_reset_yields_zero_not_negative(self):
        """A server restart resets counters; that's not negative throughput."""
        tracker = RateTracker()
        tracker.rate("k", 500.0, now=0.0)
        assert tracker.rate("k", 10.0, now=1.0) == 0.0

    def test_zero_elapsed_does_not_divide_by_zero(self):
        tracker = RateTracker()
        tracker.rate("k", 100.0, now=5.0)
        assert tracker.rate("k", 200.0, now=5.0) == 0.0

    def test_forget_drops_evicted_models(self):
        """A reloaded model must not rate against a sample from minutes ago."""
        tracker = RateTracker()
        tracker.rate("gone", 100.0, now=0.0)
        tracker.rate("kept", 100.0, now=0.0)
        tracker.forget({"kept"})

        assert tracker.rate("gone", 500.0, now=1.0) == 0.0
        assert tracker.rate("kept", 200.0, now=1.0) == pytest.approx(100.0)
