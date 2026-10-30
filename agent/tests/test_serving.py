"""Prefix-cache and speculative-decoding figures (GitHub issue #2), and the
llama.cpp KV fallback that went with them.

The llama.cpp bodies are b11977 as served on sparky 2026-10-30: no
`kv_cache_usage_ratio`, and `prompt_tokens_total` EXCLUDING cached tokens.
"""

from __future__ import annotations

import httpx
import pytest
from spark_dash_agent.collectors import serving
from spark_dash_agent.collectors.llama_router import LlamaRouterCollector
from spark_dash_common.models import (
    EngineMetrics,
    LlamaRouterMetrics,
    ModelState,
    RouterModel,
    Runtimes,
    ServingStats,
)
from test_app import make_snapshot, render
from test_engine import collect_one
from test_llama_router import model_entry

# ---------------------------------------------------------------- arithmetic


def test_zero_denominators_are_absent_not_zero():
    """Speculative decoding switched off publishes its counters at 0. That is
    "no drafts", and must not read as "0% accepted"."""
    stats = ServingStats()
    serving.fill(stats, prefix_hits=0, prefix_queries=0, draft_tokens=0, accepted=0, drafts=0)
    assert stats.prefix_hit_pct is None
    assert stats.spec_accept_pct is None
    assert stats.spec_accept_len is None
    assert stats.spec_draft_tokens_total == 0, "the counter itself is still recorded"


def test_ratios_and_length():
    stats = ServingStats()
    serving.fill(stats, prefix_hits=300, prefix_queries=1200, draft_tokens=1000, accepted=700, drafts=250)
    assert stats.prefix_hit_pct == pytest.approx(25.0)
    assert stats.spec_accept_pct == pytest.approx(70.0)
    assert stats.spec_accept_len == pytest.approx(1 + 700 / 250)


def test_unreported_counters_stay_none():
    stats = ServingStats()
    serving.fill(stats)
    assert stats.model_dump() == ServingStats().model_dump()


# ----------------------------------------------------------------- llama.cpp

B11977_BODY = """# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed, excluding cached tokens
llamacpp:prompt_tokens_total 900
llamacpp:prompt_tokens_cached_total 300
llamacpp:tokens_predicted_total 5000
llamacpp:requests_processing 1
llamacpp:requests_deferred 0
llamacpp:spec_decode_num_draft_tokens_total 1000
llamacpp:spec_decode_num_accepted_tokens_total 800
llamacpp:spec_decode_num_drafts_total 200
"""

OLD_BODY = B11977_BODY + "llamacpp:kv_cache_usage_ratio 0.5\n"

SLOTS = [
    {"id": 0, "n_ctx": 8192, "is_processing": True, "n_prompt_tokens": 2000,
     "next_token": [{"n_decoded": 48}]},
    # Idle: its context does not count, but its capacity does.
    {"id": 1, "n_ctx": 8192, "is_processing": False, "n_prompt_tokens": 0,
     "next_token": [{"n_decoded": 0}]},
]


def router(body: str, seen: list[str], slots=SLOTS, status="loaded"):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [model_entry("m", status)]})
        if request.url.path == "/props":
            return httpx.Response(200, json={})
        if request.url.path == "/metrics":
            return httpx.Response(200, text=body)
        if request.url.path == "/slots":
            return httpx.Response(200, json=slots)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def collect_llama(body: str, busy: bool = True, **kw) -> tuple[RouterModel, list[str]]:
    seen: list[str] = []
    collector = LlamaRouterCollector(
        ["http://r"], transport=router(body, seen, **kw), metrics_allowlist=["http://r"]
    )
    collector.set_busy_models({"m"} if busy else set())
    return collector.collect()[0].models[0], seen


def test_llama_prefix_hits_count_cached_tokens_as_lookups_too():
    """`prompt_tokens_total` excludes the cached ones, so 300 cached of
    900 + 300 looked up is 25%, not 300 / 900."""
    model, _ = collect_llama(B11977_BODY)
    assert model.prefix_hit_tokens_total == 300
    assert model.prefix_query_tokens_total == 1200
    assert model.prefix_hit_pct == pytest.approx(25.0)


def test_llama_speculative_decoding():
    model, _ = collect_llama(B11977_BODY)
    assert model.spec_accept_pct == pytest.approx(80.0)
    assert model.spec_accept_len == pytest.approx(5.0)


def test_llama_kv_comes_from_slots_when_the_ratio_is_gone():
    """The bug that came with issue #2: every llama.cpp model read "—"."""
    model, seen = collect_llama(B11977_BODY)
    assert "/slots" in seen
    assert model.kv_cache_pct == pytest.approx(100 * 2048 / 16384)


def test_llama_kv_ratio_is_still_preferred_where_published():
    model, seen = collect_llama(OLD_BODY)
    assert model.kv_cache_pct == pytest.approx(50.0)
    assert "/slots" not in seen, "one request per poll is enough"


def test_slots_are_never_read_for_an_idle_model():
    """Same gate as /metrics: an idle model is left to sleep."""
    model, seen = collect_llama(B11977_BODY, busy=False)
    assert "/slots" not in seen and "/metrics" not in seen
    assert model.kv_cache_pct is None


@pytest.mark.parametrize("status", ["sleeping", "unloaded"])
def test_slots_are_never_read_for_a_model_that_is_not_loaded(status):
    _, seen = collect_llama(B11977_BODY, status=status)
    assert "/slots" not in seen


def test_disabled_slots_leave_kv_empty():
    model, _ = collect_llama(B11977_BODY, slots={"error": "slots disabled"})
    assert model.kv_cache_pct is None
    assert model.prefix_hit_pct == pytest.approx(25.0), "the rest still lands"


# ------------------------------------------------------------------- engines

VLLM_BODY = """vllm:num_requests_running{model_name="qwen"} 1
vllm:num_requests_waiting{model_name="qwen"} 0
vllm:prompt_tokens_total{model_name="qwen"} 4000
vllm:generation_tokens_total{model_name="qwen"} 900
vllm:kv_cache_usage_perc{model_name="qwen"} 0.2
vllm:prefix_cache_queries_total{model_name="qwen"} 4000
vllm:prefix_cache_hits_total{model_name="qwen"} 1000
vllm:spec_decode_num_drafts_total{model_name="qwen"} 100
vllm:spec_decode_num_draft_tokens_total{model_name="qwen"} 300
vllm:spec_decode_num_accepted_tokens_total{model_name="qwen"} 150
"""

SGLANG_BODY = """sglang:num_running_reqs{model_name="ds"} 1
sglang:num_queue_reqs{model_name="ds"} 0
sglang:prompt_tokens_total{model_name="ds"} 10
sglang:generation_tokens_total{model_name="ds"} 10
sglang:cache_hit_rate{model_name="ds"} 0.93
sglang:spec_accept_length{model_name="ds"} 2.6
"""

TENSORFOLD_BODY = """tensorfold:requests_running 0
tensorfold:requests_waiting 0
tensorfold:prompt_tokens_total 10
tensorfold:generation_tokens_total 10
tensorfold:spec_decode_num_draft_tokens_total 1251005
tensorfold:spec_decode_num_accepted_tokens_total 888633
"""


def test_vllm_counters():
    _, result = collect_one("vllm", VLLM_BODY, "http://v:8000/metrics")
    assert result.prefix_hit_pct == pytest.approx(25.0)
    assert result.spec_accept_pct == pytest.approx(50.0)
    assert result.spec_accept_len == pytest.approx(2.5)


def test_sglang_reads_its_own_ratio_gauges_and_still_no_kv():
    _, result = collect_one("sglang", SGLANG_BODY, "http://s:30000/metrics")
    assert result.prefix_hit_pct == pytest.approx(93.0)
    assert result.spec_accept_len == pytest.approx(2.6)
    assert result.kv_cache_pct is None, "cache_hit_rate is prefix hits, never occupancy"
    assert result.prefix_hit_tokens_total is None, "gauges only: nothing for history"


def test_tensorfold_counts_tokens_not_steps():
    """Sparketa's numbers 2026-10-30. No draft-step counter, so no length
    rather than a guessed one, and no prefix cache series at all."""
    _, result = collect_one("tensorfold", TENSORFOLD_BODY, "http://t:8003/metrics")
    assert result.spec_accept_pct == pytest.approx(100 * 888633 / 1251005)
    assert result.spec_accept_len is None
    assert result.prefix_hit_pct is None


# ------------------------------------------------------------------ exporter


def test_counters_export_as_one_family_with_a_runtime_label():
    llama = RouterModel(name="m", state=ModelState.ACTIVE)
    serving.fill(llama, prefix_hits=300, prefix_queries=1200)
    sleeping = RouterModel(name="s", state=ModelState.SLEEPING)
    serving.fill(sleeping, prefix_hits=1, prefix_queries=2)
    tf = EngineMetrics(model="glm", server="t:8003")
    serving.fill(tf, draft_tokens=10, accepted=7)
    text = render(
        make_snapshot(
            runtimes=Runtimes(
                llama_cpp=[LlamaRouterMetrics(endpoint="http://r", name="r:8001", models=[llama, sleeping])],
                tensorfold=[tf],
            )
        )
    )
    assert (
        'sparkdash_model_prefix_cache_hit_tokens_total{model="m",node="gx10-1",'
        'runtime="llama.cpp",server="r:8001"} 300.0' in text
    )
    assert (
        'sparkdash_model_spec_accepted_tokens_total{model="glm",node="gx10-1",'
        'runtime="tensorfold",server="t:8003"} 7.0' in text
    )
    assert 'model="s"' not in text.split("sparkdash_model_prefix_cache_hit_tokens_total")[-1].split("\n#")[0], (
        "a sleeping model's last counters are not re-exported"
    )
    assert 'sparkdash_model_spec_drafts_total{' not in text, "unreported, so absent, not 0"


def test_idle_loaded_model_keeps_its_last_figures_until_it_unloads():
    """The busy gate means an idle model is not scraped, and its lifetime
    ratios read "—" most of the day. Its counters cannot move while idle, so
    the last figures stand, until it unloads and its counters start again."""
    seen: list[str] = []
    status = {"value": "loaded"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [model_entry("m", status["value"])]})
        if request.url.path == "/metrics":
            return httpx.Response(200, text=B11977_BODY)
        if request.url.path == "/slots":
            return httpx.Response(200, json=SLOTS)
        return httpx.Response(200, json={})

    collector = LlamaRouterCollector(
        ["http://r"], transport=httpx.MockTransport(handler), metrics_allowlist=["http://r"]
    )
    collector.set_busy_models({"m"})
    assert collector.collect()[0].models[0].prefix_hit_pct == pytest.approx(25.0)

    collector.set_busy_models(set())
    seen.clear()
    idle = collector.collect()[0].models[0]
    assert "/metrics" not in seen
    assert idle.prefix_hit_pct == pytest.approx(25.0)
    assert idle.spec_accept_pct == pytest.approx(80.0)

    status["value"] = "unloaded"
    collector.collect()
    status["value"] = "loaded"
    assert collector.collect()[0].models[0].prefix_hit_pct is None, "forgotten once unloaded"
