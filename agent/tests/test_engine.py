import httpx
import pytest
from spark_dash_agent.collectors.base import Budget
from spark_dash_agent.collectors.engine import (
    SPECS,
    EngineCollector,
    parse_engine_metrics,
)
from spark_dash_common.models import Runtimes

BODY = """# HELP vllm:num_requests_running Running requests
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{model_name="llama-3.3-70b"} 3.0
# HELP vllm:num_requests_waiting Waiting requests
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting{model_name="llama-3.3-70b"} 1.0
# HELP vllm:kv_cache_usage_perc KV cache usage
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{model_name="llama-3.3-70b"} 0.63
# HELP vllm:prompt_tokens Prompt tokens
# TYPE vllm:prompt_tokens counter
vllm:prompt_tokens_total{model_name="llama-3.3-70b"} 12000.0
# HELP vllm:generation_tokens Generation tokens
# TYPE vllm:generation_tokens counter
vllm:generation_tokens_total{model_name="llama-3.3-70b"} 45000.0
"""


def transport_for(body: str = BODY, status: int = 200) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status, text=body))


def test_parse_extracts_model_name_from_labels():
    """vLLM labels its series with model_name, so no extra API call is needed
    to learn what an instance is serving."""
    values, model = parse_engine_metrics(BODY)
    assert model == "llama-3.3-70b"
    assert values["vllm:num_requests_running"] == 3.0
    assert values["vllm:kv_cache_usage_perc"] == 0.63


def test_parse_normalizes_counter_total_suffix():
    """Counters arrive as `<name>_total`; lookups use the base name."""
    values, _ = parse_engine_metrics(BODY)
    assert values["vllm:prompt_tokens_total"] == 12000.0


def test_collect_converts_kv_cache_fraction_to_percent():
    """vLLM reports 0-1; the UI shows percent."""
    collector = EngineCollector(SPECS["vllm"], ["http://vllm:8000/metrics"], timeout=1.0)
    collector._endpoints = ["http://vllm:8000/metrics"]
    result = collector._collect_one(
        httpx.Client(transport=transport_for()), "http://vllm:8000/metrics", Budget(5.0)
    )
    assert result is not None
    assert result.kv_cache_pct == pytest.approx(63.0)
    assert result.requests_running == 3
    assert result.requests_waiting == 1


def test_no_endpoints_yields_empty_list():
    assert EngineCollector(SPECS["vllm"], []).collect() == []


def test_unreachable_instance_is_reported_not_dropped():
    """A configured endpoint that does not answer must still be reported.

    It used to return None and vanish, which made a typo'd port invisible: the
    node reported no vLLM, which is indistinguishable from a node that runs no
    vLLM. Silence is the failure this whole area exists to catch.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    collector = EngineCollector(SPECS["vllm"], ["http://down:8000/metrics"])
    result = collector._collect_one(
        httpx.Client(transport=httpx.MockTransport(handler)),
        "http://down:8000/metrics",
        Budget(5.0),
    )
    assert result is not None
    assert result.reachable is False
    # Labelled with the address, since nothing answered to name itself — that
    # is what lets the reader go and check the endpoint.
    assert result.server == "down:8000"
    assert result.model == "down:8000"


def test_one_instance_down_does_not_hide_the_others():
    """The original guarantee, kept: a failure is contained to its own entry."""
    body = "# TYPE vllm:num_requests_running gauge\nvllm:num_requests_running 2.0\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if "down" in str(request.url):
            raise httpx.ConnectError("refused")
        return httpx.Response(200, text=body)

    collector = EngineCollector(
        SPECS["vllm"], ["http://down:8000/metrics", "http://up:8000/metrics"]
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    results = [
        collector._collect_one(client, u, Budget(5.0))
        for u in ["http://down:8000/metrics", "http://up:8000/metrics"]
    ]
    assert [r.reachable for r in results] == [False, True]


def test_falls_back_to_url_when_model_label_missing():
    body = "# TYPE vllm:num_requests_running gauge\nvllm:num_requests_running 2.0\n"
    collector = EngineCollector(SPECS["vllm"], ["http://vllm:8000/metrics"])
    result = collector._collect_one(
        httpx.Client(transport=transport_for(body)), "http://vllm:8000/metrics", Budget(5.0)
    )
    assert result is not None
    assert result.model == "http://vllm:8000/metrics"


SGLANG_BODY = """# HELP sglang:num_running_reqs Running requests
# TYPE sglang:num_running_reqs gauge
sglang:num_running_reqs{model_name="deepseek-v3"} 2.0
# HELP sglang:num_queue_reqs Queued requests
# TYPE sglang:num_queue_reqs gauge
sglang:num_queue_reqs{model_name="deepseek-v3"} 5.0
# HELP sglang:cache_hit_rate Prefix cache hit rate
# TYPE sglang:cache_hit_rate gauge
sglang:cache_hit_rate{model_name="deepseek-v3"} 0.93
# HELP sglang:gen_throughput Generation throughput
# TYPE sglang:gen_throughput gauge
sglang:gen_throughput{model_name="deepseek-v3"} 137.5
# HELP sglang:prompt_tokens Prompt tokens
# TYPE sglang:prompt_tokens counter
sglang:prompt_tokens_total{model_name="deepseek-v3"} 8000.0
# HELP sglang:generation_tokens Generation tokens
# TYPE sglang:generation_tokens counter
sglang:generation_tokens_total{model_name="deepseek-v3"} 20000.0
"""


def collect_one(runtime: str, body: str, url: str):
    collector = EngineCollector(SPECS[runtime], [url])
    return collector, collector._collect_one(
        httpx.Client(transport=transport_for(body)), url, Budget(5.0)
    )


class TestSglang:
    """SGLang answers the same questions as vLLM under different names, which
    is why it shares the collector rather than getting a sibling of its own."""

    def test_requests_come_from_sglangs_own_metric_names(self):
        _, result = collect_one("sglang", SGLANG_BODY, "http://sglang:30000/metrics")
        assert result is not None
        assert result.requests_running == 2
        assert result.requests_waiting == 5
        assert result.model == "deepseek-v3"
        assert result.server == "sglang:30000"

    def test_cache_hit_rate_is_not_reported_as_kv_occupancy(self):
        """THE TRAP. `sglang:cache_hit_rate` is the fraction of prompt tokens
        served from the PREFIX cache — how much work was skipped, not how full
        the cache is. Same shape as vLLM's kv_cache_usage_perc, different
        question. 93% occupancy would read as a node about to evict; 93% prefix
        hits is a node doing well. An empty cell is honest, a wrong one is not.
        """
        _, result = collect_one("sglang", SGLANG_BODY, "http://sglang:30000/metrics")
        assert result is not None
        assert result.kv_cache_pct is None

    def test_prefill_and_decode_are_reported_separately(self):
        """THE defect this split exists for.

        Measured on the live cluster 2026-08-21: the combined figure reached
        47,672 tok/s while `rate(generation_tokens[5m])` peaked at 47.9. Both
        are arithmetically correct — a large prompt landing inside one poll
        window really is that fast to ingest — but only one is what a reader
        means by throughput, and a stat panel showing the sum is wrong by three
        orders of magnitude exactly when someone is watching a request arrive.
        """
        url = "http://sglang:30000/metrics"
        collector = EngineCollector(SPECS["sglang"], [url])
        collector._collect_one(
            httpx.Client(transport=transport_for(SGLANG_BODY)), url, Budget(5.0)
        )
        # A big prompt and a little generation, the shape of a prefill burst.
        later = SGLANG_BODY.replace("8000.0", "808000.0").replace("20000.0", "20050.0")
        result = collector._collect_one(
            httpx.Client(transport=transport_for(later)), url, Budget(5.0)
        )
        assert result is not None
        assert result.prompt_tokens_per_sec > result.generation_tokens_per_sec * 100, (
            "the fixture should represent a prefill burst"
        )
        # The legacy sum is still reported, and still dominated by prefill —
        # which is exactly why it is not the number anything leads with.
        assert result.tokens_per_sec == pytest.approx(
            result.generation_tokens_per_sec + result.prompt_tokens_per_sec
        )

    def test_the_fallback_gauge_counts_as_decode_not_as_a_total(self):
        """`sglang:gen_throughput` is generation throughput by definition, so
        when it stands in for missing counters it must land in the decode
        field. Filing it as a combined total would put a decode number in a
        column that means something else."""
        body = (
            "# TYPE sglang:gen_throughput gauge\n"
            'sglang:gen_throughput{model_name="deepseek-v3"} 137.5\n'
        )
        _, result = collect_one("sglang", body, "http://sglang:30000/metrics")
        assert result is not None
        assert result.generation_tokens_per_sec == pytest.approx(137.5)
        assert result.prompt_tokens_per_sec == 0.0

    def test_throughput_is_derived_from_counters_not_the_gauge(self):
        """`sglang:gen_throughput` is instantaneous decode throughput; the node
        card SUMS tokens/sec across every runtime, and that sum only means
        something if each term measures the same thing. So the counters win
        while they are present — and on a first scrape there is no previous
        sample, so the honest answer is 0 rather than the gauge's 137.5."""
        _, result = collect_one("sglang", SGLANG_BODY, "http://sglang:30000/metrics")
        assert result is not None
        assert result.generation_tokens_per_sec == 0.0
        assert result.prompt_tokens_per_sec == 0.0
        assert result.tokens_per_sec == 0.0
        assert result.prompt_tokens_total == 8000
        assert result.generation_tokens_total == 20000

    def test_second_scrape_yields_a_counter_rate(self):
        url = "http://sglang:30000/metrics"
        collector = EngineCollector(SPECS["sglang"], [url])
        client = httpx.Client(transport=transport_for(SGLANG_BODY))
        collector._collect_one(client, url, Budget(5.0))

        later = SGLANG_BODY.replace("20000.0", "20600.0")
        result = collector._collect_one(
            httpx.Client(transport=transport_for(later)), url, Budget(5.0)
        )
        assert result is not None
        assert result.generation_tokens_per_sec > 0

    def test_gauge_is_the_fallback_when_counters_are_absent(self):
        """A build that publishes no token counters still reports throughput —
        the wrong-shaped number beats no number, but only when there is no
        right-shaped one."""
        body = (
            "# TYPE sglang:num_running_reqs gauge\n"
            'sglang:num_running_reqs{model_name="deepseek-v3"} 1.0\n'
            "# TYPE sglang:gen_throughput gauge\n"
            'sglang:gen_throughput{model_name="deepseek-v3"} 137.5\n'
        )
        _, result = collect_one("sglang", body, "http://sglang:30000/metrics")
        assert result is not None
        assert result.generation_tokens_per_sec == pytest.approx(137.5)

    def test_unreachable_endpoint_is_reported_for_sglang_too(self):
        collector = EngineCollector(SPECS["sglang"], ["http://down:30000/metrics"])

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        result = collector._collect_one(
            httpx.Client(transport=httpx.MockTransport(handler)),
            "http://down:30000/metrics",
            Budget(5.0),
        )
        assert result is not None
        assert result.reachable is False
        assert result.model == "down:30000"


def test_every_spec_has_a_field_to_land_in():
    """The snapshot builder splats collected engines into `Runtimes` by
    runtime name, so a spec added without the matching field would fail only
    at runtime, on a node running that engine."""
    assert set(SPECS) <= set(Runtimes().engines)


def test_collector_is_named_for_its_runtime():
    """`safe_collect` files failures under `Collector.name`; two engines
    sharing one name would file the second's errors under the first."""
    assert {EngineCollector(spec, []).name for spec in SPECS.values()} == set(SPECS)
