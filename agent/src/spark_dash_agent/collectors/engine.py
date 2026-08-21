"""vLLM and SGLang, scraped from their native Prometheus endpoints.

Both engines answer the same questions in the same shape — a text exposition
endpoint carrying running/queued requests, token counters and a model name
label — so they share one collector and differ only by a table of metric
names. That is the abstraction runtimes actually earn: they are homogeneous in
a way the dashboard's cards are not.

Neither engine needs a sidecar; Prometheus scrapes both directly for history.
This collector exists so the agent's live snapshot is complete, letting the
backend poll one endpoint per node instead of discovering instances itself.

Unlike llama.cpp router mode, there is no autoload hazard here: scraping
/metrics is a plain read.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx
from prometheus_client.parser import text_string_to_metric_families
from spark_dash_common.models import EngineMetrics

from spark_dash_agent.collectors.base import Budget, Collector
from spark_dash_agent.collectors.llama_router import RateTracker

log = logging.getLogger(__name__)


def _host_port(url: str) -> str:
    """`http://host:8120/metrics` -> `host:8120`."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return parsed.netloc or url


@dataclass(frozen=True)
class EngineSpec:
    """What one engine calls the things every engine reports.

    Names only. Any field that needs different *handling* rather than a
    different name belongs in the collector, not here — a spec that grows
    behaviour flags has stopped being a table and become a second collector
    wearing one.
    """

    #: Matches `ProcessInfo.runtime`, so a spec and a detected process agree
    #: on one spelling. Also the metric-family prefix the exporter emits.
    runtime: str
    running: str
    waiting: str

    #: Token counters, used for throughput. Both engines publish them; the
    #: rate is computed here rather than read, so the number means the same
    #: thing for every engine on the node — see `_throughput`.
    prompt_tokens: str
    generation_tokens: str

    #: Fraction (0-1) of the KV cache in use. None for engines that do not
    #: report occupancy, which leaves the column empty rather than filled with
    #: a number that answers a different question.
    kv_cache: str | None = None

    #: An engine-reported throughput gauge, used ONLY when the counters above
    #: are absent from a scrape. See `_throughput` for why it is the fallback
    #: and not the primary.
    throughput_gauge: str | None = None


SPECS: dict[str, EngineSpec] = {
    "vllm": EngineSpec(
        runtime="vllm",
        running="vllm:num_requests_running",
        waiting="vllm:num_requests_waiting",
        prompt_tokens="vllm:prompt_tokens_total",
        generation_tokens="vllm:generation_tokens_total",
        kv_cache="vllm:kv_cache_usage_perc",
    ),
    "sglang": EngineSpec(
        runtime="sglang",
        running="sglang:num_running_reqs",
        waiting="sglang:num_queue_reqs",
        prompt_tokens="sglang:prompt_tokens_total",
        generation_tokens="sglang:generation_tokens_total",
        # NO kv_cache. `sglang:cache_hit_rate` is the fraction of prompt
        # tokens served from the PREFIX cache — how much work was skipped, not
        # how full the cache is. Same shape, different question, and putting it
        # in this column would render a number that reads as occupancy and is
        # not. `sglang:token_usage` is the closer analogue and is a candidate
        # once it can be checked against a running server; until then an empty
        # cell is honest and a guessed one is not.
        kv_cache=None,
        throughput_gauge="sglang:gen_throughput",
    ),
}


def parse_engine_metrics(text: str) -> tuple[dict[str, float], str | None]:
    """Extract every sample by name, plus the served model name.

    Both engines label their series with `model_name`, which is how we learn
    what a given instance is serving without a separate API call.
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


class EngineCollector(Collector[list[EngineMetrics]]):
    """One engine's configured endpoints, scraped concurrently."""

    def __init__(
        self,
        spec: EngineSpec,
        endpoints: list[str],
        *,
        timeout: float = 2.0,
        budget_s: float = 5.0,
    ) -> None:
        self._spec = spec
        self.name = spec.runtime
        self._endpoints = endpoints
        self._timeout = timeout
        self._budget_s = budget_s
        self._rates = RateTracker()

    def collect(self) -> list[EngineMetrics]:
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
                    max_workers=len(self._endpoints), thread_name_prefix=self._spec.runtime
                ) as pool:
                    results = list(
                        pool.map(
                            lambda u: self._collect_one(client, u, budget), self._endpoints
                        )
                    )
        return [m for m in results if m is not None]

    def _collect_one(
        self, client: httpx.Client, url: str, budget: Budget
    ) -> EngineMetrics | None:
        spec = self._spec
        try:
            if budget.spent:
                raise TimeoutError("collection budget spent before this endpoint")
            resp = client.get(url, timeout=budget.timeout(self._timeout))
            resp.raise_for_status()
            values, model_name = parse_engine_metrics(resp.text)
        except Exception:  # noqa: BLE001 — one instance down shouldn't hide the rest
            log.debug("%s scrape failed for %s", spec.runtime, url, exc_info=True)
            # REPORTED, NOT DROPPED. Returning None here made a typo'd port
            # invisible: the node reported no instance, which reads exactly
            # like a node that runs none. Silence is the failure this area
            # exists to catch, so a configured endpoint that did not answer
            # comes back as an entry saying so.
            #
            # `model` carries the endpoint because nothing answered to name
            # itself, and a row labelled with the address is what lets the
            # reader go and check it.
            return EngineMetrics(
                model=_host_port(url),
                server=_host_port(url),
                reachable=False,
            )

        prompt = values.get(spec.prompt_tokens, 0.0)
        generation = values.get(spec.generation_tokens, 0.0)
        kv = values.get(spec.kv_cache) if spec.kv_cache else None

        return EngineMetrics(
            model=model_name or url,
            # host:port, so it sits in the same column as a llama.cpp router
            # rather than leaving a gap. Nothing fronts these engines, so an
            # instance's own endpoint IS where the model is served from.
            server=_host_port(url),
            requests_running=int(values.get(spec.running, 0)),
            requests_waiting=int(values.get(spec.waiting, 0)),
            # Reported as a 0-1 fraction; the UI wants percent.
            kv_cache_pct=kv * 100.0 if kv is not None else None,
            tokens_per_sec=self._throughput(url, values, prompt, generation),
            prompt_tokens_total=int(prompt),
            generation_tokens_total=int(generation),
        )

    def _throughput(
        self, url: str, values: dict[str, float], prompt: float, generation: float
    ) -> float:
        """Tokens/sec, from the counters where possible.

        DERIVED, NOT READ, even though SGLang publishes `gen_throughput`
        directly. The node card sums this across every runtime on the node, and
        that sum is only meaningful if each term measures the same thing:
        `gen_throughput` is instantaneous DECODE throughput over the engine's
        last batch, while the counter rate is prompt+generation over the poll
        interval, which is what vLLM and the llama.cpp routers already
        contribute. Adding the two together would produce a total that is
        neither.

        The gauge is still the better answer than nothing, so it is used when
        the counters are missing from a scrape — an engine build that does not
        publish them, or one scraped before it has served a request.
        """
        spec = self._spec
        if spec.prompt_tokens in values or spec.generation_tokens in values:
            return self._rates.rate(f"{url}:prompt", prompt) + self._rates.rate(
                f"{url}:generation", generation
            )
        if spec.throughput_gauge:
            return values.get(spec.throughput_gauge, 0.0)
        return 0.0
