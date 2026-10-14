"""Time to first token for the header (roadmap AT)."""

from __future__ import annotations

import asyncio

from spark_dash_backend.latency import GATEWAY_TTFT, TTFT, latency
from spark_dash_backend.prometheus import Series

TF = "10.0.0.2:8003"
ENGINES = {TF: {"model": "GLM-5.3-Flash-EXL3", "node": "sparketa", "runtime": "tensorfold"}}


def s(labels, value):
    return Series(labels=labels, points=[(0.0, float(value))])


class Prom:
    """Answers by which histogram and which statistic a query asks for."""

    def __init__(self, engine=None, gateway=(), accept=None):
        self.engine = engine or {}  # stat -> value for the TensorFold instance
        self.gateway = list(gateway)  # (model, requested_model, {stat: value})
        self.accept = accept
        self.asked: list[str] = []

    async def query(self, expr):
        self.asked.append(expr)
        stat = "p50" if "histogram_quantile(0.5," in expr else "p95" if "histogram_quantile(0.95," in expr else "rate"
        if "mtp_accepted" in expr:
            return [s({"instance": TF}, self.accept)] if self.accept is not None else []
        if TTFT["tensorfold"] in expr:
            v = self.engine.get(stat)
            return [s({"instance": TF}, v)] if v is not None else []
        if GATEWAY_TTFT in expr:
            return [s({"model": m, "requested_model": r}, f[stat]) for m, r, f in self.gateway if stat in f]
        return []


def run(prom, gateway=True):
    return asyncio.run(latency(prom, ENGINES, gateway))


def test_an_engine_instance_is_named_by_its_model():
    body = run(Prom(engine={"p50": 1.54, "p95": 2.48, "rate": 0.2}, accept=0.71))
    (m,) = body["models"]
    assert (m["model"], m["node"], m["runtime"], m["source"]) == ("GLM-5.3-Flash-EXL3", "sparketa", "tensorfold", "engine")
    assert (m["p50_s"], m["p95_s"], m["per_min"], m["accept"]) == (1.54, 2.48, 12.0, 0.71)
    assert body["busiest"] == m


def test_the_gateway_fills_in_only_for_models_no_engine_measures():
    """The same requests measured twice from two vantage points would be
    counted twice; a llama.cpp model has no histogram but the gateway's."""
    body = run(Prom(
        engine={"p50": 1.5, "p95": 2.5, "rate": 0.2},
        gateway=[("GLM-5.3-Flash-EXL3", "danflashes/GLM-5.3-Flash-EXL3", {"p50": 1.6, "p95": 2.6, "rate": 0.2}),
                 ("qwen36-35b", "sparky-prod/qwen36-35b", {"p50": 0.4, "p95": 0.9, "rate": 0.5})],
    ))
    by = {m["model"]: m["source"] for m in body["models"]}
    assert by == {"GLM-5.3-Flash-EXL3": "engine", "sparky-prod/qwen36-35b": "gateway"}


def test_the_busiest_model_leads():
    body = run(Prom(
        engine={"p50": 1.5, "p95": 2.5, "rate": 0.2},
        gateway=[("qwen36-35b", "sparky-prod/qwen36-35b", {"p50": 0.4, "p95": 0.9, "rate": 0.5})],
    ))
    assert body["busiest"]["model"] == "sparky-prod/qwen36-35b"
    assert [m["model"] for m in body["models"]] == ["sparky-prod/qwen36-35b", "GLM-5.3-Flash-EXL3"]


def test_an_idle_window_has_no_headline():
    """No requests: the histogram's quantile is NaN, and no number is better
    than a stale one."""
    body = run(Prom(engine={"p50": float("nan"), "p95": float("nan"), "rate": 0.0}))
    assert body["busiest"] is None
    (m,) = body["models"]
    assert (m["p50_s"], m["per_min"]) == (None, 0.0)


def test_without_a_gateway_it_is_not_asked():
    prom = Prom(engine={"p50": 1.5, "p95": 2.5, "rate": 0.2})
    run(prom, gateway=False)
    assert not any(GATEWAY_TTFT in q for q in prom.asked)


def test_quantiles_are_per_instance_never_pooled():
    """Engines' bucket boundaries differ, and a quantile over buckets summed
    across different boundaries is not a quantile of anything."""
    prom = Prom(engine={"p50": 1.5, "p95": 2.5, "rate": 0.2})
    run(prom)
    for q in prom.asked:
        if q.startswith("histogram_quantile(") and GATEWAY_TTFT not in q:
            assert "sum by (le, instance)" in q, q
