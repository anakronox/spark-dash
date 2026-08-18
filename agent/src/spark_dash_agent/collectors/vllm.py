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
from concurrent.futures import ThreadPoolExecutor

import httpx
from prometheus_client.parser import text_string_to_metric_families
from spark_dash_common.models import VllmMetrics

from spark_dash_agent.collectors.base import Budget, Collector
from spark_dash_agent.collectors.llama_router import RateTracker

log = logging.getLogger(__name__)


def _host_port(url: str) -> str:
    """`http://host:8120/metrics` -> `host:8120`."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return parsed.netloc or url

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

    def __init__(
        self, endpoints: list[str], *, timeout: float = 2.0, budget_s: float = 5.0
    ) -> None:
        self._endpoints = endpoints
        self._timeout = timeout
        self._budget_s = budget_s
        self._rates = RateTracker()

    def collect(self) -> list[VllmMetrics]:
        if not self._endpoints:
            return []

        # Concurrent for the same reason as the llama routers: sequentially,
        # each unresponsive endpoint added its whole timeout to the snapshot's
        # critical path, so the worst case scaled with how much the node ran.
        budget = Budget(self._budget_s)
        with httpx.Client(timeout=self._timeout) as client:
            if len(self._endpoints) == 1:
                results = [self._collect_one(client, self._endpoints[0], budget)]
            else:
                with ThreadPoolExecutor(
                    max_workers=len(self._endpoints), thread_name_prefix="vllm"
                ) as pool:
                    results = list(
                        pool.map(
                            lambda u: self._collect_one(client, u, budget), self._endpoints
                        )
                    )
        return [m for m in results if m is not None]

    def _collect_one(
        self, client: httpx.Client, url: str, budget: Budget
    ) -> VllmMetrics | None:
        try:
            if budget.spent:
                raise TimeoutError("collection budget spent before this endpoint")
            resp = client.get(url, timeout=budget.timeout(self._timeout))
            resp.raise_for_status()
            values, model_name = parse_vllm_metrics(resp.text)
        except Exception:  # noqa: BLE001 — one instance down shouldn't hide the rest
            log.debug("vllm scrape failed for %s", url, exc_info=True)
            # REPORTED, NOT DROPPED. Returning None here made a typo'd port
            # invisible: the node reported no vLLM, which reads exactly like a
            # node that runs no vLLM. Silence is the failure this area exists
            # to catch, so a configured endpoint that did not answer comes back
            # as an entry saying so.
            #
            # `model` carries the endpoint because nothing answered to name
            # itself, and a row labelled with the address is what lets the
            # reader go and check it.
            return VllmMetrics(
                model=_host_port(url),
                server=_host_port(url),
                reachable=False,
            )

        prompt = values.get(_M_PROMPT_TOKENS, 0.0)
        generation = values.get(_M_GENERATION_TOKENS, 0.0)
        kv = values.get(_M_KV_CACHE)

        return VllmMetrics(
            model=model_name or url,
            # host:port, so it sits in the same column as a llama.cpp router
            # rather than leaving a gap. Nothing fronts a vLLM instance, so its
            # own endpoint IS where the model is served from.
            server=_host_port(url),
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
