import httpx
import pytest
from spark_dash_agent.collectors.base import Budget
from spark_dash_agent.collectors.vllm import VllmCollector, parse_vllm_metrics

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
    values, model = parse_vllm_metrics(BODY)
    assert model == "llama-3.3-70b"
    assert values["vllm:num_requests_running"] == 3.0
    assert values["vllm:kv_cache_usage_perc"] == 0.63


def test_parse_normalizes_counter_total_suffix():
    """Counters arrive as `<name>_total`; lookups use the base name."""
    values, _ = parse_vllm_metrics(BODY)
    assert values["vllm:prompt_tokens_total"] == 12000.0


def test_collect_converts_kv_cache_fraction_to_percent():
    """vLLM reports 0-1; the UI shows percent."""
    collector = VllmCollector(["http://vllm:8000/metrics"], timeout=1.0)
    collector._endpoints = ["http://vllm:8000/metrics"]
    result = collector._collect_one(
        httpx.Client(transport=transport_for()), "http://vllm:8000/metrics", Budget(5.0)
    )
    assert result is not None
    assert result.kv_cache_pct == pytest.approx(63.0)
    assert result.requests_running == 3
    assert result.requests_waiting == 1


def test_no_endpoints_yields_empty_list():
    assert VllmCollector([]).collect() == []


def test_unreachable_instance_is_reported_not_dropped():
    """A configured endpoint that does not answer must still be reported.

    It used to return None and vanish, which made a typo'd port invisible: the
    node reported no vLLM, which is indistinguishable from a node that runs no
    vLLM. Silence is the failure this whole area exists to catch.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    collector = VllmCollector(["http://down:8000/metrics"])
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

    collector = VllmCollector(["http://down:8000/metrics", "http://up:8000/metrics"])
    client = httpx.Client(transport=httpx.MockTransport(handler))
    results = [
        collector._collect_one(client, u, Budget(5.0))
        for u in ["http://down:8000/metrics", "http://up:8000/metrics"]
    ]
    assert [r.reachable for r in results] == [False, True]


def test_falls_back_to_url_when_model_label_missing():
    body = "# TYPE vllm:num_requests_running gauge\nvllm:num_requests_running 2.0\n"
    collector = VllmCollector(["http://vllm:8000/metrics"])
    result = collector._collect_one(
        httpx.Client(transport=transport_for(body)), "http://vllm:8000/metrics", Budget(5.0)
    )
    assert result is not None
    assert result.model == "http://vllm:8000/metrics"
