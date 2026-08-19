"""The critical property under test: we must never request
`/metrics?model=X` for a model that isn't already ACTIVE.

Doing so triggers autoload and resets the idle-sleep timer
(ggml-org/llama.cpp#23096). Both GX10 routers run `--models-autoload`, so this
is a live hazard: a careless scrape loop would wake every registered model and
hold it resident, exhausting the shared memory pool. Monitoring would be the
thing that breaks the box.

Payload fixtures are taken verbatim from llama.cpp b10380 on the GX10.
"""

import socket
import threading
import time

import httpx
import pytest
from spark_dash_agent.collectors.base import Budget
from spark_dash_agent.collectors.llama_router import (
    LlamaRouterCollector,
    RateTracker,
    parse_model_meta,
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
        collector = LlamaRouterCollector(
            ["http://r"], transport=router.transport(), metrics_allowlist=["http://r"]
        )
        # Both gates must be open: allowlisted AND doing real GPU work.
        collector.set_busy_models({"active-one", "sleeper", "cold"})
        collector.collect()

        assert len(router.metrics_requests) == 1
        assert "active-one" in router.metrics_requests[0]
        assert not any("sleeper" in u or "cold" in u for u in router.metrics_requests)

    def test_loaded_but_idle_model_is_not_scraped(self):
        """The regression this replaced, and it cost real memory.

        `/metrics?model=` resets the router's idle timer. Measured on the GX10:
        a model that had slept for 171 minutes under `/v1/models` and `/props`
        polling stayed ACTIVE for 25+ minutes with a completely flat token
        counter once metrics scraping was enabled — polling every couple of
        seconds against a 1200s timeout meant the timer could never expire, and
        26.4 GiB stayed pinned indefinitely.

        A loaded model with no GPU work must therefore be left alone, so its
        idle timer runs out and the router can reclaim the memory.
        """
        router = RecordingRouter([model_entry("idle-but-loaded", "loaded")])
        collector = LlamaRouterCollector(
            ["http://r"], transport=router.transport(), metrics_allowlist=["http://r"]
        )
        # Allowlisted and ACTIVE, but doing no work — the busy set is empty.
        collector.collect()

        assert router.metrics_requests == []

    def test_busy_set_is_not_a_substitute_for_the_allowlist(self):
        """Both gates independently. A model can be working hard on a router
        the operator never opted in, and it still must not be touched."""
        router = RecordingRouter([model_entry("busy", "loaded")])
        collector = LlamaRouterCollector(["http://r"], transport=router.transport())
        collector.set_busy_models({"busy"})
        collector.collect()

        assert router.metrics_requests == []

    def test_unknown_status_is_not_scraped(self):
        """A future llama.cpp inventing a new status must not be able to trick
        us into waking a model."""
        router = RecordingRouter([model_entry("mystery", "some-future-state")])
        results = LlamaRouterCollector(["http://r"], transport=router.transport()).collect()

        assert router.metrics_requests == []
        assert results[0].models[0].state is ModelState.UNKNOWN
        # The verbatim value is retained so this is diagnosable, not silent.
        assert results[0].models[0].raw_status == "some-future-state"

    def test_scraping_is_off_by_default(self):
        """The default must be safe: an ACTIVE model on a router that wasn't
        explicitly opted in is still never requested."""
        router = RecordingRouter([model_entry("active-one", "loaded")])
        results = LlamaRouterCollector(["http://r"], transport=router.transport()).collect()

        assert router.metrics_requests == []
        # State is still fully visible — only the per-model detail is withheld.
        assert results[0].models[0].state is ModelState.ACTIVE

    def test_allowlist_is_per_router(self):
        """The 8108 scenario: opting a small-model router in must not opt in
        the one hosting 70B models."""

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [model_entry("m", "loaded")]})
            if request.url.path == "/props":
                return httpx.Response(200, json=PROPS_BODY)
            return httpx.Response(200, text=METRICS_BODY)

        requested: list[str] = []
        collector = LlamaRouterCollector(
            ["http://small:8001", "http://huge:8108"],
            transport=httpx.MockTransport(handler),
            metrics_allowlist=["http://small:8001"],
        )
        collector.set_busy_models({"m"})
        collector.collect()

        metrics_urls = [u for u in requested if "/metrics" in u]
        assert len(metrics_urls) == 1
        assert "small:8001" in metrics_urls[0]
        assert not any("huge:8108" in u for u in metrics_urls)

    def test_allowlisted_router_still_skips_non_active_models(self):
        """Both conditions are required — opting a router in does not authorize
        touching its sleeping models."""
        router = RecordingRouter(
            [model_entry("sleeper", "sleeping"), model_entry("cold", "unloaded")]
        )
        LlamaRouterCollector(
            ["http://r"], transport=router.transport(), metrics_allowlist=["http://r"]
        ).collect()

        assert router.metrics_requests == []

    @pytest.mark.parametrize("plausible", ["ready", "active", "running"])
    def test_unobserved_status_values_are_not_treated_as_active(self, plausible):
        """Only "loaded" has been observed to mean weights-resident. These are
        plausible synonyms, but acting on a guess against a 70B model could
        exhaust the shared pool — so they map to UNKNOWN and are never
        scraped."""
        router = RecordingRouter([model_entry("big-70b", plausible)])
        results = LlamaRouterCollector(
            ["http://r"], transport=router.transport(), metrics_allowlist=["http://r"]
        ).collect()

        assert router.metrics_requests == []
        assert results[0].models[0].state is ModelState.UNKNOWN
        # Retained verbatim so an unrecognized value is obvious in the UI.
        assert results[0].models[0].raw_status == plausible

    def test_real_gx10_router_8001_mix(self):
        """Verbatim from the GX10's 8001 router with one model loaded: only the
        loaded model may be touched, and only when the router is opted in."""
        router = RecordingRouter(
            [
                model_entry("cydonia-24b", "sleeping"),
                model_entry("gemma4-26b", "sleeping"),
                model_entry("qwen36-35b", "loaded"),
            ]
        )
        collector = LlamaRouterCollector(
            ["http://192.168.50.61:8001"],
            transport=router.transport(),
            metrics_allowlist=["http://192.168.50.61:8001"],
        )
        collector.set_busy_models({"qwen36-35b", "cydonia-24b", "gemma4-26b"})
        result = collector.collect()[0]

        assert [m.name for m in result.active_models] == ["qwen36-35b"]
        assert [m.name for m in result.sleeping_models] == ["cydonia-24b", "gemma4-26b"]
        assert len(router.metrics_requests) == 1
        assert "qwen36-35b" in router.metrics_requests[0]

    def test_real_gx10_router_8001_mix_untouched_by_default(self):
        """Same payload, no allowlist: the loaded model is still reported, but
        nothing is requested."""
        router = RecordingRouter(
            [model_entry("qwen36-35b", "loaded"), model_entry("cydonia-24b", "sleeping")]
        )
        result = LlamaRouterCollector(
            ["http://192.168.50.61:8001"], transport=router.transport()
        ).collect()[0]

        assert [m.name for m in result.active_models] == ["qwen36-35b"]
        assert router.metrics_requests == []


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
            # --- confirmed against llama.cpp b10380 on the GX10 ---
            ("loaded", ModelState.ACTIVE),
            ("sleeping", ModelState.SLEEPING),
            ("unloaded", ModelState.UNLOADED),
            # --- inferred, but all non-scrapeable so the risk is nil ---
            ("loading", ModelState.LOADING),
            ("stopped", ModelState.UNLOADED),
            # --- plausible but never observed: must NOT be ACTIVE ---
            ("active", ModelState.UNKNOWN),
            ("running", ModelState.UNKNOWN),
            ("ready", ModelState.UNKNOWN),
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
        collector = LlamaRouterCollector(
            ["http://r"], transport=router.transport(), metrics_allowlist=["http://r"]
        )
        collector.set_busy_models({"m"})
        result = collector.collect()[0]
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


def test_scrapeable_states_contains_only_active():
    """A guard on the safety invariant itself: if someone widens this set, the
    blast radius is loading a multi-billion-parameter model into a shared pool.
    """
    from spark_dash_common.models import SCRAPEABLE_STATES

    assert frozenset({ModelState.ACTIVE}) == SCRAPEABLE_STATES

class TestCollectionIsBounded:
    """Q2/Q3, 2026-08-18.

    A router that is loading a model does not refuse connections — it accepts
    them and then does not answer. Sequential collection turned that into a
    stall that grew with every runtime the node served: two routers at three
    requests each, 2s apiece, is a 12s worst case from a "2 second timeout".
    Collection then outlived Prometheus's 10s scrape timeout and the backend's
    3s poll timeout, and the node vanished from the dashboard.
    """

    @staticmethod
    def _slow_transport(delay_s: float) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            time.sleep(delay_s)
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [model_entry("m", "loaded")]})
            if request.url.path == "/props":
                return httpx.Response(200, json=PROPS_BODY)
            return httpx.Response(200, text=METRICS_BODY)

        return httpx.MockTransport(handler)

    def test_routers_are_polled_concurrently(self):
        """Four slow routers must cost about one router's time, not four.

        This is the property that stops the worst case scaling with the
        cluster: C adds nodes, and each node may add routers."""
        delay = 0.25
        urls = [f"http://r{i}:8080" for i in range(4)]
        collector = LlamaRouterCollector(urls, transport=self._slow_transport(delay))

        started = time.monotonic()
        result = collector.collect()
        elapsed = time.monotonic() - started

        assert len(result) == 4
        sequential = delay * 2 * 4  # /v1/models + /props per router, at minimum
        assert elapsed < sequential * 0.6, (
            f"{elapsed:.2f}s for 4 routers looks sequential (>= {sequential:.2f}s)"
        )

    def test_router_order_survives_concurrency(self):
        """The snapshot's router order must be the CONFIGURED order, not
        whichever thread finished first — otherwise the dashboard's rows
        reshuffle between ticks for no reason the reader can see."""
        urls = [f"http://r{i}:8080" for i in range(4)]
        collector = LlamaRouterCollector(urls, transport=self._slow_transport(0.02))
        assert [r.endpoint for r in collector.collect()] == urls

    def test_the_budget_shrinks_the_timeout_ACTUALLY_SENT(self):
        """Deterministic proof of the mechanism.

        httpx records the effective timeout on each request, so this asserts
        what the collector asked the network for rather than how long a fake
        happened to take. A 0.4s budget must not issue 2s requests."""
        seen: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.extensions["timeout"]["read"])
            return httpx.Response(200, json={"data": []})

        LlamaRouterCollector(
            ["http://r0:8080", "http://r1:8080"],
            timeout=2.0,
            budget_s=0.4,
            transport=httpx.MockTransport(handler),
        ).collect()

        assert seen, "no requests were issued"
        assert max(seen) <= 0.4, f"a request asked for {max(seen)}s against a 0.4s budget"

    def test_the_budget_bounds_a_router_that_never_answers(self):
        """The real shape of the failure, against a real socket.

        A router loading a model does not refuse the connection — it accepts
        and goes quiet, which is why a connect timeout never fired and the read
        timeout was the one that mattered. This binds a socket that accepts and
        never writes, which is that behaviour exactly.
        """
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(8)
        port = server.getsockname()[1]
        accepted = []

        def accept_and_go_quiet():
            while True:
                try:
                    accepted.append(server.accept()[0])
                except OSError:
                    return

        threading.Thread(target=accept_and_go_quiet, daemon=True).start()
        try:
            collector = LlamaRouterCollector(
                [f"http://127.0.0.1:{port}"] * 4,
                timeout=2.0,
                budget_s=0.75,
                transport=None,
            )
            started = time.monotonic()
            result = collector.collect()
            elapsed = time.monotonic() - started
        finally:
            server.close()
            for conn in accepted:
                conn.close()

        # Measured: 0.75s with the budget, 2.02s without it (the routers run
        # concurrently either way, so unbounded costs one full read timeout).
        # The threshold sits between those, not next to the failing value.
        assert elapsed < 1.5, f"collection took {elapsed:.2f}s against a 0.75s budget"
        assert all(not r.reachable for r in result), (
            "a router that could not answer within the budget is unreachable "
            "for this tick, not silently dropped"
        )

    def test_a_spent_budget_skips_the_request_entirely(self):
        """Once the allowance is gone, no new request is even issued — the
        cheap half of the guarantee, and the one that keeps a node with many
        runtimes from queueing work it has no time for."""
        budget = Budget(0.0)
        assert budget.spent
        assert budget.timeout(2.0) == 0.0

    def test_the_budget_shrinks_a_request_timeout_to_what_is_left(self):
        budget = Budget(0.5)
        assert budget.timeout(2.0) == pytest.approx(0.5, abs=0.05)
        assert budget.timeout(0.1) == pytest.approx(0.1, abs=0.01), (
            "a request asking for less than remains keeps its own ceiling"
        )


class TestModelMeta:
    """What a model IS, from the `meta` block llama.cpp already returns.

    Fixture values are verbatim from cydonia-24b on the production router,
    2026-08-19.
    """

    REAL = {
        "id": "cydonia-24b",
        "meta": {
            "vocab_type": 2,
            "n_vocab": 131072,
            "n_ctx": 131072,
            "n_ctx_train": 131072,
            "n_embd": 5120,
            "n_params": 23572403200,
            "size": 16756101120,
            "ftype": "Q5_K - Medium",
        },
    }

    def test_parses_the_real_payload(self):
        assert parse_model_meta(self.REAL) == {
            "size_bytes": 16756101120,
            "n_params": 23572403200,
            "quantization": "Q5_K - Medium",
            "context_length": 131072,
        }

    def test_absent_meta_yields_nothing_rather_than_zeros(self):
        """vLLM has no equivalent and older llama.cpp omits the block. On a card
        whose job is telling you how big a model is, "unknown" and "zero bytes"
        must not look alike."""
        assert parse_model_meta({"id": "x"}) == {}
        assert parse_model_meta({"id": "x", "meta": "not-a-dict"}) == {}

    def test_a_bad_field_costs_that_field_only(self):
        """`size` has been seen as a string. One unparseable value should not
        take the whole model row with it."""
        out = parse_model_meta({"meta": {"size": "nope", "n_ctx": 4096}})
        assert out["size_bytes"] is None
        assert out["context_length"] == 4096

    def test_numeric_strings_are_accepted(self):
        assert parse_model_meta({"meta": {"size": "123"}})["size_bytes"] == 123

    def test_negative_values_are_rejected(self):
        assert parse_model_meta({"meta": {"n_ctx": -5}})["context_length"] is None

    def test_discovery_carries_meta_onto_the_model(self):
        """The wiring, not just the parser."""
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [dict(TestModelMeta.REAL, **{
                    "status": {"value": "loaded"}})]})
            return httpx.Response(200, json=PROPS_BODY)

        collector = LlamaRouterCollector(["http://r"], transport=httpx.MockTransport(handler))
        model = collector.collect()[0].models[0]
        assert model.size_bytes == 16756101120
        assert model.quantization == "Q5_K - Medium"
        assert model.context_length == 131072
