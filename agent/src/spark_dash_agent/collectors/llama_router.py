"""llama.cpp router-mode metrics.

THE CONSTRAINT THAT SHAPES THIS WHOLE MODULE: in router mode,
`GET /metrics?model=X` triggers autoload of X and resets its idle-sleep timer
(ggml-org/llama.cpp#23096). A naive scrape loop would therefore load every
registered model and hold them all resident forever — actively fighting the
router's own LRU eviction and exhausting the shared memory pool. Monitoring
would be the thing that breaks the box.

So: discover which models are *already loaded* using an endpoint that takes no
`model` parameter, and only then fetch per-model metrics for those. Never probe
a model to find out whether it's loaded.

There is also no aggregated all-models endpoint in router mode
(ggml-org/llama.cpp#19197), which is why the fan-out lives here.
"""

from __future__ import annotations

import logging
import time

import httpx
from prometheus_client.parser import text_string_to_metric_families
from spark_dash_common.models import LlamaRouterMetrics, LoadedModel

from spark_dash_agent.collectors.base import Collector

log = logging.getLogger(__name__)

# llama.cpp server metric names (the `llamacpp:` namespace).
_M_PROMPT_TOKENS = "llamacpp:prompt_tokens_total"
_M_PREDICTED_TOKENS = "llamacpp:tokens_predicted_total"
_M_REQUESTS_PROCESSING = "llamacpp:requests_processing"
_M_REQUESTS_DEFERRED = "llamacpp:requests_deferred"
_M_KV_CACHE_RATIO = "llamacpp:kv_cache_usage_ratio"


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
        """Drop state for models that are no longer loaded.

        Without this, a model evicted and later reloaded would compute its first
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


class LlamaRouterCollector(Collector[LlamaRouterMetrics]):
    """Polls a local llama.cpp router.

    Reports `None` when no router is configured or reachable, so the same agent
    image runs unchanged on a node that only serves vLLM.
    """

    name = "llama_router"

    def __init__(
        self,
        base_url: str | None,
        *,
        timeout: float = 2.0,
        scrape_loaded_model_metrics: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") if base_url else None
        self._timeout = timeout
        # Escape hatch: if this router build turns out to autoload even on the
        # discovery path, set this False to keep the loaded-model list without
        # ever touching /metrics.
        self._scrape_metrics = scrape_loaded_model_metrics
        # Test seam — lets the suite assert which URLs are actually requested,
        # which is how the "never wake a sleeping model" guarantee is verified.
        self._transport = transport
        self._rates = RateTracker()

    def collect(self) -> LlamaRouterMetrics | None:
        if not self._base_url:
            return None

        with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
            models = self._discover_models(client)
            if models is None:
                return None

            loaded_names = [name for name, loaded in models if loaded]
            loaded: list[LoadedModel] = []
            for name in loaded_names:
                loaded.append(self._collect_model(client, name))

        self._rates.forget(set(loaded_names))
        return LlamaRouterMetrics(
            loaded_models=loaded,
            known_model_count=len(models),
            tokens_per_sec=sum(m.tokens_per_sec or 0.0 for m in loaded),
        )

    def _discover_models(self, client: httpx.Client) -> list[tuple[str, bool]] | None:
        """List models and whether each is resident, without triggering a load.

        Uses `/v1/models`, which takes no `model` parameter and so cannot
        autoload. The response shape varies across llama.cpp builds, so the
        loaded flag is read defensively from several plausible keys.

        Fail-safe: when residency can't be determined, models are reported as
        NOT loaded. That costs us some metrics but guarantees we never probe a
        sleeping model awake.
        """
        try:
            resp = client.get(f"{self._base_url}/v1/models")
            resp.raise_for_status()
            payload = resp.json()
        except Exception:  # noqa: BLE001 — router absent or not in router mode
            log.debug("llama router model discovery failed", exc_info=True)
            return None

        entries = payload.get("data", payload if isinstance(payload, list) else [])
        models: list[tuple[str, bool]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("id") or entry.get("name")
            if not name:
                continue
            models.append((str(name), _is_loaded(entry)))
        return models

    def _collect_model(self, client: httpx.Client, name: str) -> LoadedModel:
        model = LoadedModel(name=name)
        if not self._scrape_metrics:
            return model

        # Safe *only* because `name` came from the loaded set above.
        try:
            resp = client.get(f"{self._base_url}/metrics", params={"model": name})
            resp.raise_for_status()
            metrics = parse_model_metrics(resp.text)
        except Exception:  # noqa: BLE001 — model may have been evicted mid-scrape
            log.debug("metrics fetch failed for model %s", name, exc_info=True)
            return model

        predicted = metrics.get(_M_PREDICTED_TOKENS, 0.0)
        prompt = metrics.get(_M_PROMPT_TOKENS, 0.0)

        model.requests_running = int(metrics.get(_M_REQUESTS_PROCESSING, 0))
        model.requests_waiting = int(metrics.get(_M_REQUESTS_DEFERRED, 0))
        kv = metrics.get(_M_KV_CACHE_RATIO)
        model.kv_cache_pct = kv * 100.0 if kv is not None else None
        model.tokens_per_sec = self._rates.rate(f"{name}:predicted", predicted) + self._rates.rate(
            f"{name}:prompt", prompt
        )
        return model


def _is_loaded(entry: dict) -> bool:
    """Read residency from a `/v1/models` entry, defaulting to not-loaded.

    Key names differ across llama.cpp builds and this is the decision that keeps
    us from tripping the autoload bug, so an unrecognized shape must fail safe.
    """
    for key in ("loaded", "is_loaded", "resident"):
        if key in entry:
            return bool(entry[key])
    state = entry.get("state") or entry.get("status")
    if isinstance(state, str):
        return state.lower() in ("loaded", "ready", "active", "running")
    return False
