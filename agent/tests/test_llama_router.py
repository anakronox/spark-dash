"""The critical property under test: we must never request
`/metrics?model=X` for a model that isn't already ACTIVE.

Doing so triggers autoload and resets the idle-sleep timer
(ggml-org/llama.cpp#23096). Both GX10 routers run `--models-autoload`, so this
is a live hazard: a careless scrape loop would wake every registered model and
hold it resident, exhausting the shared memory pool. Monitoring would be the
thing that breaks the box.

Payload fixtures are taken verbatim from llama.cpp b10380 on the GX10.
"""

import httpx
import pytest
from spark_dash_agent.collectors.llama_router import (
    LlamaRouterCollector,
    RateTracker,
    parse_model_metrics,
    parse_model_state,
)
from spark_dash_common.models import ModelState

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

PROPS_BODY = {
    "role": "router",
    "max_instances": 3,
    "models_autoload": True,
    "model_alias": "llama-server",
    "build_info": "b10380-0b1bad14f",
}


def model_entry(name: str, status_value: str) -> dict:
    """A /v1/models entry in the real b10380 shape.

    Note `status` is an OBJECT with a nested `value` — an earlier version of
    this collector assumed a plain string and therefore reported every model as
    not-loaded, forever.
    """
    return {
        "id": name,
        "aliases": [],
        "object": "model",
        "owned_by": "llamacpp",
        "status": {"value": status_value, "args": ["/app/llama-server", "--alias", name]},
        "source": "preset",
    }


class RecordingRouter:
    """Fake router that records every path requested."""

    def __init__(self, models: list[dict], props: dict | None = None):
        self.models = models
        self.props = PROPS_BODY if props is None else props
        self.requested: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requested.append(str(request.url))
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": self.models})
            if request.url.path == "/props":
                return httpx.Response(200, json=self.props)
            if request.url.path == "/metrics":
                return httpx.Response(200, text=METRICS_BODY)
            return httpx.Response(404)

        return httpx.MockTransport(handler)

    @property
    def metrics_requests(self) -> list[str]:
        return [u for u in self.requested if "/metrics" in u]


class TestNeverWakeASleepingModel:
    """The safety property this module exists to guarantee."""

    def test_sleeping_models_are_never_scraped(self):
        """The exact GX10 situation: models slept after --sleep-idle-seconds.
        Their processes are alive, so they answer /v1/models instantly — but
        touching /metrics would reload the weights."""
        router = RecordingRouter(
            [
                model_entry("cydonia-24b", "sleeping"),
                model_entry("gemma4-26b", "sleeping"),
                model_entry("qwen36-35b", "sleeping"),
            ]
        )
        results = LlamaRouterCollector(["http://r:8001"], transport=router.transport()).collect()

        assert router.metrics_requests == []
        assert len(results[0].models) == 3
        assert all(m.state is ModelState.SLEEPING for m in results[0].models)

    def test_unloaded_models_are_never_scraped(self):
        router = RecordingRouter(
            [model_entry("anubis70b", "unloaded"), model_entry("hermes3-70b", "unloaded")]
        )
        results = LlamaRouterCollector(["http://r:8108"], transport=router.transport()).collect()

        assert router.metrics_requests == []
        assert all(m.state is ModelState.UNLOADED for m in results[0].models)

    def test_only_the_active_model_is_scraped(self):
        router = RecordingRouter(
            [
                model_entry("active-one", "loaded"),
                model_entry("sleeper", "sleeping"),
                model_entry("cold", "unloaded"),
            ]
        )
        LlamaRouterCollector(["http://r"], transport=router.transport()).collect()

        assert len(router.metrics_requests) == 1
        assert "active-one" in router.metrics_requests[0]
        assert not any("sleeper" in u or "cold" in u for u in router.metrics_requests)

    def test_unknown_status_is_not_scraped(self):
        """A future llama.cpp inventing a new status must not be able to trick
        us into waking a model."""
        router = RecordingRouter([model_entry("mystery", "some-future-state")])
        results = LlamaRouterCollector(["http://r"], transport=router.transport()).collect()

        assert router.metrics_requests == []
        assert results[0].models[0].state is ModelState.UNKNOWN
        # The verbatim value is retained so this is diagnosable, not silent.
        assert results[0].models[0].raw_status == "some-future-state"

    def test_scrape_disabled_touches_nothing(self):
        router = RecordingRouter([model_entry("active-one", "loaded")])
        collector = LlamaRouterCollector(
            ["http://r"], transport=router.transport(), scrape_loaded_model_metrics=False
        )
        results = collector.collect()

        assert router.metrics_requests == []
        assert results[0].models[0].state is ModelState.ACTIVE


class TestParseModelState:
    def test_reads_nested_status_value(self):
        """`status` is an object, not a string — the bug that made this always
        report zero loaded models."""
        state, raw = parse_model_state(model_entry("m", "sleeping"))
        assert state is ModelState.SLEEPING
        assert raw == "sleeping"

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("active", ModelState.ACTIVE),
            ("loaded", ModelState.ACTIVE),
            ("ready", ModelState.ACTIVE),
            ("running", ModelState.ACTIVE),
            ("sleeping", ModelState.SLEEPING),
            ("idle", ModelState.SLEEPING),
            ("loading", ModelState.LOADING),
            ("unloaded", ModelState.UNLOADED),
            ("stopped", ModelState.UNLOADED),
            ("wat", ModelState.UNKNOWN),
        ],
    )
    def test_status_mapping(self, value, expected):
        assert parse_model_state(model_entry("m", value))[0] is expected

    def test_case_and_whitespace_tolerant(self):
        entry = {"id": "m", "status": {"value": " SLEEPING "}}
        assert parse_model_state(entry)[0] is ModelState.SLEEPING

    def test_plain_string_status_still_works(self):
        assert parse_model_state({"id": "m", "status": "loaded"})[0] is ModelState.ACTIVE

    def test_boolean_shape_still_works(self):
        """Older/simpler builds may expose a boolean instead of a status object."""
        assert parse_model_state({"id": "m", "loaded": True})[0] is ModelState.ACTIVE
        assert parse_model_state({"id": "m", "loaded": False})[0] is ModelState.UNLOADED

    def test_no_status_information_is_unknown_not_active(self):
        """Absence of evidence must never be read as 'safe to scrape'."""
        assert parse_model_state({"id": "m"})[0] is ModelState.UNKNOWN


class TestRouterProperties:
    def test_captures_max_instances_and_autoload(self):
        """max_instances is --models-max; it's what makes '2 of 3 slots'
        expressible."""
        router = RecordingRouter([model_entry("m", "sleeping")])
        result = LlamaRouterCollector(["http://r"], transport=router.transport()).collect()[0]

        assert result.max_instances == 3
        assert result.autoload is True

    def test_missing_props_is_not_fatal(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/props":
                return httpx.Response(404)
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [model_entry("m", "sleeping")]})
            return httpx.Response(404)

        result = LlamaRouterCollector(
            ["http://r"], transport=httpx.MockTransport(handler)
        ).collect()[0]

        assert result.max_instances is None
        assert result.reachable is True
        assert len(result.models) == 1

    def test_state_groupings(self):
        router = RecordingRouter(
            [
                model_entry("a", "loaded"),
                model_entry("b", "sleeping"),
                model_entry("c", "sleeping"),
                model_entry("d", "unloaded"),
            ]
        )
        result = LlamaRouterCollector(["http://r"], transport=router.transport()).collect()[0]

        assert result.known_model_count == 4
        assert [m.name for m in result.active_models] == ["a"]
        assert [m.name for m in result.sleeping_models] == ["b", "c"]


class TestActiveModelMetrics:
    def test_parses_metrics_for_active_model(self):
        router = RecordingRouter([model_entry("m", "loaded")])
        result = LlamaRouterCollector(["http://r"], transport=router.transport()).collect()[0]
        model = result.models[0]

        assert model.requests_running == 2
        assert model.requests_waiting == 1
        assert model.kv_cache_pct == pytest.approx(63.0)

    def test_parse_model_metrics_flattens_families(self):
        values = parse_model_metrics(METRICS_BODY)
        assert values["llamacpp:tokens_predicted_total"] == 5000.0
        assert values["llamacpp:kv_cache_usage_ratio"] == 0.63


class TestMultipleRouters:
    """The GX10 runs two routers on the same host, different ports."""

    def test_polls_every_configured_router(self):
        def handler(request: httpx.Request) -> httpx.Response:
            name = "model-a" if request.url.port == 8001 else "model-b"
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [model_entry(name, "sleeping")]})
            if request.url.path == "/props":
                return httpx.Response(200, json=PROPS_BODY)
            return httpx.Response(404)

        results = LlamaRouterCollector(
            ["http://192.168.50.61:8001", "http://192.168.50.61:8108"],
            transport=httpx.MockTransport(handler),
        ).collect()

        assert [r.name for r in results] == ["192.168.50.61:8001", "192.168.50.61:8108"]
        assert results[0].models[0].name == "model-a"
        assert results[1].models[0].name == "model-b"

    def test_one_router_down_does_not_hide_the_others(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "dead":
                raise httpx.ConnectError("refused")
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [model_entry("ok", "sleeping")]})
            return httpx.Response(200, json=PROPS_BODY)

        results = LlamaRouterCollector(
            ["http://dead:8080", "http://alive:8081"], transport=httpx.MockTransport(handler)
        ).collect()

        assert results[0].reachable is False
        assert results[1].reachable is True
        assert results[1].models[0].name == "ok"

    def test_trailing_slashes_are_normalized(self):
        router = RecordingRouter([model_entry("m", "sleeping")])
        results = LlamaRouterCollector(["http://r:8080/"], transport=router.transport()).collect()
        assert results[0].endpoint == "http://r:8080"
        assert all("//v1/models" not in u for u in router.requested)


def test_returns_empty_when_no_routers_configured():
    """vLLM-only nodes run the same image with no router URLs set."""
    assert LlamaRouterCollector([]).collect() == []


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

    def test_forget_drops_models_that_slept(self):
        """A model that slept and later woke must not rate against a stale
        sample from before it slept."""
        tracker = RateTracker()
        tracker.rate("gone", 100.0, now=0.0)
        tracker.rate("kept", 100.0, now=0.0)
        tracker.forget({"kept"})

        assert tracker.rate("gone", 500.0, now=1.0) == 0.0
        assert tracker.rate("kept", 200.0, now=1.0) == pytest.approx(100.0)
