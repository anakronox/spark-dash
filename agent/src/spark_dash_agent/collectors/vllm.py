"""vLLM metrics, scraped from its native Prometheus endpoint.

vLLM needs no sidecar of its own — Prometheus scrapes it directly for history.
This collector exists purely so the agent's live snapshot is complete, letting
the backend poll one endpoint per node instead of discovering vLLM instances
itself.

Unlike llama.cpp router mode, there is no autoload hazard here: scraping vLLM's
/metrics is a plain read.
"""

from __future__ import annotations

import logging

import httpx
from prometheus_client.parser import text_string_to_metric_families
from spark_dash_common.models import VllmMetrics

from spark_dash_agent.collectors.base import Collector
from spark_dash_agent.collectors.llama_router import RateTracker

log = logging.getLogger(__name__)

_M_RUNNING = "vllm:num_requests_running"
_M_WAITING = "vllm:num_requests_waiting"
_M_KV_CACHE = "vllm:kv_cache_usage_perc"
_M_PROMPT_TOKENS = "vllm:prompt_tokens_total"
_M_GENERATION_TOKENS = "vllm:generation_tokens_total"


def parse_vllm_metrics(text: str) -> tuple[dict[str, float], str | None]:
    """Extract the metrics we care about, plus the served model name.

    vLLM labels its series with `model_name`, which is how we learn what a given
    instance is serving without a separate API call.
    """
    values: dict[str, float] = {}
    model_name: str | None = None

    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if model_name is None:
                model_name = sample.labels.get("model_name") or sample.labels.get("model")
            # Counters arrive as `<name>_total`; normalize so lookups are stable.
            key = sample.name.removesuffix("_total") if sample.name.endswith("_total") else None
            for candidate in (sample.name, key):
                if candidate:
                    values.setdefault(candidate, sample.value)
    return values, model_name


class VllmCollector(Collector[list[VllmMetrics]]):
    name = "vllm"

    def __init__(self, endpoints: list[str], *, timeout: float = 2.0) -> None:
        self._endpoints = endpoints
        self._timeout = timeout
        self._rates = RateTracker()

    def collect(self) -> list[VllmMetrics]:
        if not self._endpoints:
            return []

        out: list[VllmMetrics] = []
        with httpx.Client(timeout=self._timeout) as client:
            for url in self._endpoints:
                metrics = self._collect_one(client, url)
                if metrics is not None:
                    out.append(metrics)
        return out

    def _collect_one(self, client: httpx.Client, url: str) -> VllmMetrics | None:
        try:
            resp = client.get(url)
            resp.raise_for_status()
            values, model_name = parse_vllm_metrics(resp.text)
        except Exception:  # noqa: BLE001 — one instance down shouldn't hide the rest
            log.debug("vllm scrape failed for %s", url, exc_info=True)
            return None

        prompt = values.get(_M_PROMPT_TOKENS, 0.0)
        generation = values.get(_M_GENERATION_TOKENS, 0.0)
        kv = values.get(_M_KV_CACHE)

        return VllmMetrics(
            model=model_name or url,
            requests_running=int(values.get(_M_RUNNING, 0)),
            requests_waiting=int(values.get(_M_WAITING, 0)),
            # vLLM reports this as a 0-1 fraction; the UI wants percent.
            kv_cache_pct=kv * 100.0 if kv is not None else None,
            tokens_per_sec=(
                self._rates.rate(f"{url}:prompt", prompt)
                + self._rates.rate(f"{url}:generation", generation)
            ),
            prompt_tokens_total=int(prompt),
            generation_tokens_total=int(generation),
        )
