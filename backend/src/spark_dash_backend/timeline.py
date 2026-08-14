"""Model lifecycle history — when models loaded, slept and unloaded.

Reconstructed from Prometheus rather than stored separately. The agent already
exports `sparkdash_llama_model_state{node,router,model,state}` as a one-hot
series, so every transition is already in the TSDB — this just reads it back.
Nothing new to persist, and history reaches back as far as retention does
rather than starting from whenever this feature shipped.

Why it's worth having: a router swap is a user-visible latency spike. The live
view shows a model is loaded now, but not that it was evicted twenty minutes
ago and reloaded — which is the actual explanation when someone says a request
felt slow.

A caveat worth knowing: at a 15s scrape interval, a state that exists for less
than one scrape can be missed entirely. That's fine in practice because
`--sleep-idle-seconds` is 300-1200s here, so real transitions are two orders of
magnitude slower than the sampling. A LOADING state may well be skipped over,
though, since loading is quick relative to a scrape.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from spark_dash_backend.prometheus import PrometheusClient, PrometheusError

log = logging.getLogger(__name__)

STATE_METRIC = "sparkdash_llama_model_state"

# Transitions worth surfacing, and how to describe them. Keyed (from, to).
#
# Only changes that mean something operationally are named. A model going
# unloaded -> sleeping isn't a thing the router does, so unnamed pairs fall
# back to a generic description rather than being silently dropped: an
# unexpected transition is more interesting than a familiar one, not less.
TRANSITIONS: dict[tuple[str, str], str] = {
    ("unloaded", "loading"): "loading",
    ("unloaded", "active"): "loaded",
    ("sleeping", "loading"): "waking",
    ("sleeping", "active"): "woke",
    ("loading", "active"): "loaded",
    ("active", "sleeping"): "slept",
    ("active", "unloaded"): "evicted",
    ("sleeping", "unloaded"): "released",
    ("loading", "unloaded"): "load failed",
    ("active", "unknown"): "lost track",
}

# Transitions that cost a user real latency: the next request to this model
# waits for weights to come off disk.
COLD_TRANSITIONS = {"evicted", "released", "load failed"}


@dataclass
class ModelEvent:
    ts: float
    node: str
    router: str
    model: str
    from_state: str
    to_state: str
    label: str
    """True when the transition means the next request pays a load penalty."""
    cold: bool

    def as_dict(self) -> dict:
        return {
            "ts": self.ts,
            "node": self.node,
            "router": self.router,
            "model": self.model,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "label": self.label,
            "cold": self.cold,
        }


def _series_key(labels: dict[str, str]) -> tuple[str, str, str]:
    return (
        labels.get("node", "?"),
        labels.get("router", "?"),
        labels.get("model", "?"),
    )


def extract_events(series: list, *, ignore_first_sample: bool = True) -> list[ModelEvent]:
    """Turn one-hot state series into transition events.

    The metric is one series per (model, state) carrying 1 or 0, so a model's
    state at time t is whichever of its series is 1. This inverts that into a
    single state per timestamp, then reports the points where it changes.

    `ignore_first_sample` skips the transition into the first observed state.
    Otherwise every model in the window appears to have "just loaded" at the
    left edge of the chart, which is an artifact of where the window starts
    rather than something that happened.
    """
    # (node, router, model) -> {timestamp -> state}
    by_model: dict[tuple[str, str, str], dict[float, str]] = {}

    for s in series:
        labels = getattr(s, "labels", {}) or {}
        state = labels.get("state")
        if not state:
            continue
        key = _series_key(labels)
        timeline = by_model.setdefault(key, {})
        for ts, value in getattr(s, "points", []):
            # Only the series holding 1 identifies the state at that instant.
            if value == 1.0:
                timeline[ts] = state

    events: list[ModelEvent] = []
    for (node, router, model), timeline in by_model.items():
        previous: str | None = None
        for ts in sorted(timeline):
            state = timeline[ts]
            if previous is None:
                previous = state
                if ignore_first_sample:
                    continue
            if state == previous:
                continue

            label = TRANSITIONS.get((previous, state), f"{previous} → {state}")
            events.append(
                ModelEvent(
                    ts=ts,
                    node=node,
                    router=router,
                    model=model,
                    from_state=previous,
                    to_state=state,
                    label=label,
                    cold=label in COLD_TRANSITIONS,
                )
            )
            previous = state

    events.sort(key=lambda e: e.ts, reverse=True)
    return events


async def fetch_events(
    prom: PrometheusClient,
    *,
    start: float,
    end: float,
    step: str = "60s",
) -> list[ModelEvent]:
    """Read model transitions over a window.

    Step size is a real tradeoff: too coarse and brief states vanish between
    samples, too fine and Prometheus returns far more points than there are
    transitions. 60s suits sleep timers measured in minutes.
    """
    try:
        series = await prom.query_range(STATE_METRIC, start, end, step)
    except PrometheusError:
        log.debug("model timeline query failed", exc_info=True)
        raise

    return extract_events(series)
