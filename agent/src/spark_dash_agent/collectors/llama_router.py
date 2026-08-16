"""llama.cpp router-mode metrics.

THE CONSTRAINT THAT SHAPES THIS WHOLE MODULE: in router mode,
`GET /metrics?model=X` triggers autoload of X and resets its idle-sleep timer
(ggml-org/llama.cpp#23096). Both routers on the GX10 run with
`--models-autoload`, so this is live, not theoretical. A naive scrape loop would
load every registered model and hold them resident forever — fighting the
router's own eviction and exhausting the shared memory pool. Monitoring would be
the thing that breaks the box.

So: read every model's state from `/v1/models` (which takes no `model`
parameter and cannot autoload), and fetch per-model metrics ONLY for models
already active. A sleeping model is deliberately left alone.

There is also no aggregated all-models endpoint in router mode
(ggml-org/llama.cpp#19197), which is why the fan-out lives here.

Response shapes confirmed against llama.cpp b10380.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

import httpx
from prometheus_client.parser import text_string_to_metric_families
from spark_dash_common.models import (
    SCRAPEABLE_STATES,
    LlamaRouterMetrics,
    ModelState,
    RouterModel,
)

from spark_dash_agent.collectors.base import Collector

log = logging.getLogger(__name__)

# llama.cpp server metric names (the `llamacpp:` namespace).
_M_PROMPT_TOKENS = "llamacpp:prompt_tokens_total"
_M_PREDICTED_TOKENS = "llamacpp:tokens_predicted_total"
_M_REQUESTS_PROCESSING = "llamacpp:requests_processing"
_M_REQUESTS_DEFERRED = "llamacpp:requests_deferred"
_M_KV_CACHE_RATIO = "llamacpp:kv_cache_usage_ratio"

# Router `status.value` strings → our state enum.
#
# All three states below are CONFIRMED against llama.cpp b10380 on the GX10:
# "loaded", "sleeping", "unloaded".
#
# ACTIVE contains EXACTLY ONE value, and that asymmetry is deliberate. Mapping a
# status to ACTIVE authorizes a `/metrics?model=` request, which on an autoload
# router LOADS the model — potentially tens of GB into a shared pool. So only a
# string observed to mean "weights resident and serving" earns that mapping.
#
# Earlier drafts also mapped "active", "running" and "ready" to ACTIVE on
# plausibility alone. They are removed: none was ever observed, and "ready" in
# particular could just as easily mean "ready to BE loaded". If a future
# llama.cpp uses one of them, this maps it to UNKNOWN — the model shows up as
# unknown-state and is left untouched, which is wrong but safe, and the verbatim
# string in `raw_status` makes it obvious what to add here.
_STATUS_MAP = {
    # --- confirmed: weights resident and serving ---
    "loaded": ModelState.ACTIVE,
    # --- confirmed: process alive, weights released after --sleep-idle-seconds ---
    "sleeping": ModelState.SLEEPING,
    # --- confirmed: no child process ---
    "unloaded": ModelState.UNLOADED,
    # --- inferred, but harmless: none of these are scrapeable states ---
    "loading": ModelState.LOADING,
    "starting": ModelState.LOADING,
    "stopped": ModelState.UNLOADED,
}


class RateTracker:
    """Turns monotonic counters into a per-second rate.

    Only for the live view — Prometheus computes its own `rate()` for history.
    A counter that goes backwards (server restart) yields 0 rather than a
    nonsensical negative spike.
    """

    def __init__(self) -> None:
        self._previous: dict[str, tuple[float, float]] = {}

    def rate(self, key: str, value: float, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        prev = self._previous.get(key)
        self._previous[key] = (now, value)

        if prev is None:
            return 0.0
        prev_time, prev_value = prev
        elapsed = now - prev_time
        if elapsed <= 0 or value < prev_value:
            return 0.0
        return (value - prev_value) / elapsed

    def forget(self, keep_keys: set[str]) -> None:
        """Drop state for models no longer active.

        Without this, a model that slept and later woke would compute its first
        rate against a stale sample from minutes ago.
        """
        for key in set(self._previous) - keep_keys:
            del self._previous[key]


def parse_model_metrics(text: str) -> dict[str, float]:
    """Flatten a llama.cpp `/metrics` response into {metric_name: value}."""
    out: dict[str, float] = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            out[sample.name] = sample.value
    return out


def parse_model_state(entry: dict) -> tuple[ModelState, str]:
    """Read a model's state from a `/v1/models` entry.

    The router nests this as `status.value` — `status` is an OBJECT, not a
    string. Several shapes are accepted anyway because this decision determines
    whether we're allowed to touch a model, and it must keep failing safe
    across llama.cpp versions.

    Returns the mapped state plus the verbatim string, so an unrecognized value
    is diagnosable in the UI instead of vanishing into UNKNOWN.
    """
    raw = ""
    for key in ("status", "state"):
        field = entry.get(key)
        if isinstance(field, dict):
            raw = str(field.get("value") or "")
        elif isinstance(field, str):
            raw = field
        if raw:
            break

    # Older/simpler builds may expose a plain boolean instead.
    if not raw:
        for key in ("loaded", "is_loaded", "resident"):
            if key in entry:
                loaded = bool(entry[key])
                return (
                    ModelState.ACTIVE if loaded else ModelState.UNLOADED,
                    f"{key}={entry[key]}",
                )
        return ModelState.UNKNOWN, ""

    return _STATUS_MAP.get(raw.strip().lower(), ModelState.UNKNOWN), raw


class LlamaRouterCollector(Collector[list[LlamaRouterMetrics]]):
    """Polls every configured llama.cpp router on this node.

    Plural by design: a node commonly runs several router containers. Returns an
    empty list when none are configured, so the same agent image runs unchanged
    on a node that only serves vLLM.
    """

    name = "llama_router"

    def __init__(
        self,
        base_urls: list[str],
        *,
        timeout: float = 2.0,
        metrics_allowlist: list[str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_urls = [u.rstrip("/") for u in base_urls if u]
        self._timeout = timeout

        # Per-router opt-in for `/metrics?model=` requests, EMPTY BY DEFAULT.
        #
        # This is the safety boundary. A router not on this list is only ever
        # read via /v1/models and /props, neither of which takes a model
        # parameter and neither of which can cause a load. Opting in is a
        # per-router decision because the blast radius differs: waking a 12B
        # model is a nuisance, waking a 70B one on a shared 128GB pool can
        # exhaust the node and take down everything else running on the GPU.
        self._metrics_allowlist = {u.rstrip("/") for u in (metrics_allowlist or []) if u}
        for url in self._metrics_allowlist:
            log.warning(
                "per-model metrics scraping ENABLED for %s — this issues "
                "/metrics?model= requests, which load the model if the router "
                "reports a state we treat as active",
                url,
            )

        # Models with real GPU work happening right now, set by the snapshot
        # builder from NVML's per-process SM samples before each collect.
        #
        # THIS IS WHY THE MODEL CAN STILL SLEEP. A `/metrics?model=` request
        # resets the router's idle timer — measured on the GX10, where a model
        # that had previously slept for 171 minutes under `/v1/models` and
        # `/props` polling stayed active for 25+ minutes with a completely flat
        # token counter once metrics scraping was switched on. Polling every
        # couple of seconds against a 1200s timeout meant the timer could never
        # expire, and the model held 26.4 GiB indefinitely.
        #
        # SM utilization comes from NVML, which the router cannot see, so
        # gating on it breaks that feedback loop rather than trading one
        # problem for another: an idle model stops being polled and its timer
        # runs out normally, while a busy one is scraped and would have had its
        # timer reset by the inference itself anyway.
        #
        # Empty means scrape nothing. That is deliberate — if the GPU collector
        # fails we lose throughput numbers rather than silently pinning every
        # loaded model in memory.
        self._busy_models: set[str] = set()

        # Test seam — lets the suite assert which URLs are actually requested,
        # which is how the "never wake a sleeping model" guarantee is verified.
        self._transport = transport
        self._rates = RateTracker()

    def set_busy_models(self, names: set[str]) -> None:
        """Which models have GPU work in flight, from NVML's per-process view.

        Called before `collect()`. Kept as state rather than a `collect()`
        argument so the `Collector.safe_collect` contract stays uniform across
        every collector.
        """
        self._busy_models = names

    def collect(self) -> list[LlamaRouterMetrics]:
        if not self._base_urls:
            return []

        out: list[LlamaRouterMetrics] = []
        live_rate_keys: set[str] = set()

        with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
            for base_url in self._base_urls:
                result = self._collect_router(client, base_url)
                out.append(result)
                live_rate_keys |= {
                    f"{base_url}:{m.name}:{suffix}"
                    for m in result.active_models
                    for suffix in ("predicted", "prompt")
                }

        self._rates.forget(live_rate_keys)
        return out

    def _collect_router(self, client: httpx.Client, base_url: str) -> LlamaRouterMetrics:
        models = self._discover_models(client, base_url)
        if models is None:
            # One router being down must not hide the others.
            return LlamaRouterMetrics(endpoint=base_url, name=_label_for(base_url), reachable=False)

        for model in models:
            if model.state in SCRAPEABLE_STATES:
                self._enrich_active_model(client, base_url, model)

        props = self._fetch_props(client, base_url)
        return LlamaRouterMetrics(
            endpoint=base_url,
            name=_label_for(base_url),
            reachable=True,
            models=models,
            max_instances=props.get("max_instances"),
            autoload=props.get("models_autoload"),
            tokens_per_sec=sum(m.tokens_per_sec or 0.0 for m in models),
        )

    def _fetch_props(self, client: httpx.Client, base_url: str) -> dict:
        """Router-level properties: `max_instances` (`--models-max`) and whether
        autoload is on.

        `max_instances` is what makes "2 of 3 slots used" expressible. Safe to
        call — without a `model` parameter this describes the router itself and
        cannot wake anything.
        """
        try:
            resp = client.get(f"{base_url}/props")
            resp.raise_for_status()
            payload = resp.json()
        except Exception:  # noqa: BLE001 — optional enrichment
            log.debug("props fetch failed for %s", base_url, exc_info=True)
            return {}
        return payload if isinstance(payload, dict) else {}

    def _discover_models(self, client: httpx.Client, base_url: str) -> list[RouterModel] | None:
        """List every registered model and its state, without triggering a load.

        `/v1/models` takes no `model` parameter, so it cannot autoload.
        """
        try:
            resp = client.get(f"{base_url}/v1/models")
            resp.raise_for_status()
            payload = resp.json()
        except Exception:  # noqa: BLE001 — router absent or not in router mode
            log.debug("llama router model discovery failed for %s", base_url, exc_info=True)
            return None

        entries = payload.get("data", payload if isinstance(payload, list) else [])
        models: list[RouterModel] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("id") or entry.get("name")
            if not name:
                continue
            state, raw = parse_model_state(entry)
            models.append(RouterModel(name=str(name), state=state, raw_status=raw))
        return models

    def _enrich_active_model(self, client: httpx.Client, base_url: str, model: RouterModel) -> None:
        """Fetch metrics for an ACTIVE model on an allowlisted router.

        TWO independent conditions must both hold before this issues a request:
        the router is explicitly opted in, and the model is already ACTIVE.
        Either one alone would be enough in theory; requiring both means a
        mistake in the status mapping cannot by itself load a model on a router
        the operator never opted in.
        """
        if base_url not in self._metrics_allowlist:
            return

        # Busy right now? See `_busy_models`. Without this the scrape itself
        # keeps the model awake forever.
        if model.name not in self._busy_models:
            return

        try:
            resp = client.get(f"{base_url}/metrics", params={"model": model.name})
            resp.raise_for_status()
            metrics = parse_model_metrics(resp.text)
        except Exception:  # noqa: BLE001 — model may have slept mid-scrape
            log.debug("metrics fetch failed for model %s", model.name, exc_info=True)
            return

        model.requests_running = int(metrics.get(_M_REQUESTS_PROCESSING, 0))
        model.requests_waiting = int(metrics.get(_M_REQUESTS_DEFERRED, 0))
        kv = metrics.get(_M_KV_CACHE_RATIO)
        model.kv_cache_pct = kv * 100.0 if kv is not None else None

        # Rate keys are namespaced by router: the same model name can be
        # registered with more than one router on a node.
        model.tokens_per_sec = self._rates.rate(
            f"{base_url}:{model.name}:predicted", metrics.get(_M_PREDICTED_TOKENS, 0.0)
        ) + self._rates.rate(f"{base_url}:{model.name}:prompt", metrics.get(_M_PROMPT_TOKENS, 0.0))


def _label_for(base_url: str) -> str:
    """Short host:port label so the UI can tell routers apart."""
    parsed = urlparse(base_url)
    return parsed.netloc or base_url
