"""Time to first token, per model, for the header (roadmap AT).

Throughput is drawn everywhere and latency nowhere, though most of it was
already stored: the engines Prometheus scrapes directly publish a
time-to-first-token histogram, and the LiteLLM gateway publishes its own.

PER ENGINE INSTANCE, NEVER POOLED. Each engine has its own bucket boundaries
(TensorFold's start at 10 ms, LiteLLM's at 5 ms), and a quantile over buckets
summed across different boundaries is not a quantile of anything. So each
instance gets its own p50 and p95, and the header shows the busiest model's.

THE GATEWAY FILLS THE GAP, AND ONLY THE GAP. A llama.cpp router publishes
timing counters, which give an average and no percentile, so a model served
there has a latency only through the gateway, when one is configured. A model
an engine already measures is not read from the gateway as well: it would be
the same requests measured twice, from two vantage points.

Speculative decoding acceptance rides along where an engine reports it:
accepted draft tokens over drafted ones, in the same window.
"""

from __future__ import annotations

import asyncio
import math

from .prometheus import PrometheusClient, Series

#: Long enough that an agent's bursts are inside it. At five minutes the
#: header read "idle" through most of a working session here.
WINDOW = "15m"

#: Engine runtime -> its time-to-first-token histogram, as scraped. SGLang's is
#: from its documentation; there is no SGLang engine here to confirm it on.
TTFT: dict[str, str] = {
    "vllm": "vllm:time_to_first_token_seconds",
    "sglang": "sglang:time_to_first_token_seconds",
    "tensorfold": "tensorfold:time_to_first_token_seconds",
}

#: Engine runtime -> (accepted, drafted) counters for speculative decoding.
SPECULATIVE: dict[str, tuple[str, str]] = {
    "vllm": ("vllm:spec_decode_num_accepted_tokens_total", "vllm:spec_decode_num_draft_tokens_total"),
    "tensorfold": ("tensorfold:mtp_accepted_total", "tensorfold:mtp_drafted_total"),
}

GATEWAY_TTFT = "litellm_llm_api_time_to_first_token_metric"


def _value(series: list[Series], key: str, labels_key) -> dict[str, float]:
    out: dict[str, float] = {}
    for s in series:
        if s.points and not math.isnan(s.points[-1][1]):
            out[labels_key(s.labels)] = s.points[-1][1]
    return out


def _quantiles(metric: str, by: str, window: str) -> tuple[str, str, str]:
    bucket = f"sum by (le, {by}) (rate({metric}_bucket[{window}]))"
    return (
        f"histogram_quantile(0.5, {bucket})",
        f"histogram_quantile(0.95, {bucket})",
        f"sum by ({by}) (rate({metric}_count[{window}]))",
    )


async def latency(prom: PrometheusClient, engines: dict[str, dict], gateway: bool) -> dict:
    """`engines`: an engine's `host:port` -> {model, node, runtime}, from the
    live snapshot, which is what names an instance: TensorFold labels no
    series with its model."""
    queries: list[tuple[str, str, str]] = []
    for runtime, metric in TTFT.items():
        p50, p95, rate = _quantiles(metric, "instance", WINDOW)
        queries += [(runtime, "p50", p50), (runtime, "p95", p95), (runtime, "rate", rate)]
    for runtime, (acc, drafted) in SPECULATIVE.items():
        queries.append((runtime, "accept",
                        f"sum by (instance) (rate({acc}[{WINDOW}])) / sum by (instance) (rate({drafted}[{WINDOW}]))"))
    if gateway:
        p50, p95, rate = _quantiles(GATEWAY_TTFT, "model, requested_model", WINDOW)
        queries += [("gateway", "p50", p50), ("gateway", "p95", p95), ("gateway", "rate", rate)]

    answers = await asyncio.gather(*(prom.query(q) for _, _, q in queries), return_exceptions=True)

    by_instance: dict[str, dict] = {}
    by_gateway: dict[str, dict] = {}
    for (source, field, _), series in zip(queries, answers):
        if isinstance(series, BaseException):
            continue
        if source == "gateway":
            values = _value(series, field, lambda l: f"{l.get('model', '')}\x00{l.get('requested_model', '')}")
            for k, v in values.items():
                by_gateway.setdefault(k, {})[field] = v
        else:
            for inst, v in _value(series, field, lambda l: l.get("instance", "")).items():
                by_instance.setdefault(inst, {"runtime": source})[field] = v

    models: list[dict] = []
    measured: set[str] = set()
    for inst, f in by_instance.items():
        if "rate" not in f and "p50" not in f:
            continue  # acceptance alone, with no requests to put it beside
        known = engines.get(inst, {})
        model = known.get("model") or inst
        measured.add(model)
        models.append({
            "model": model,
            "node": known.get("node"),
            "runtime": known.get("runtime") or f["runtime"],
            "source": "engine",
            "p50_s": f.get("p50"),
            "p95_s": f.get("p95"),
            "per_min": round(f.get("rate", 0.0) * 60, 2),
            "accept": f.get("accept"),
        })
    for key, f in by_gateway.items():
        model, requested = key.split("\x00", 1)
        if model in measured or requested.split("/", 1)[-1] in measured:
            continue
        models.append({
            "model": requested or model,
            "node": None,
            "runtime": None,
            "source": "gateway",
            "p50_s": f.get("p50"),
            "p95_s": f.get("p95"),
            "per_min": round(f.get("rate", 0.0) * 60, 2),
            "accept": None,
        })

    models.sort(key=lambda m: -m["per_min"])
    active = [m for m in models if m["per_min"] > 0 and m["p50_s"] is not None]
    return {
        "window": WINDOW,
        "busiest": active[0] if active else None,
        "models": models,
    }
