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


def _timelines(series: list) -> dict[tuple[str, str, str], dict[float, str]]:
    """Invert one-hot state series into {(node, router, model): {ts: state}}.

    The metric is one series per (model, state) carrying 1 or 0, so a model's
    state at an instant is whichever of its series is 1.

    Shared by the event extractor and the load summariser so the two cannot
    disagree about what state a model was in at a given moment.
    """
    by_model: dict[tuple[str, str, str], dict[float, str]] = {}
    for s in series:
        labels = getattr(s, "labels", {}) or {}
        state = labels.get("state")
        if not state:
            continue
        timeline = by_model.setdefault(_series_key(labels), {})
        for ts, value in getattr(s, "points", []):
            if value == 1.0:
                timeline[ts] = state
    return by_model


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
    by_model = _timelines(series)

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
    return extract_events(await fetch_state_series(prom, start=start, end=end, step=step))


async def fetch_state_series(
    prom: PrometheusClient,
    *,
    start: float,
    end: float,
    step: str = "60s",
) -> list:
    """The raw one-hot state series, for callers that want more than events.

    Split out so transitions and load durations can be derived from ONE fetch
    rather than querying the same series twice for two views of it.
    """
    try:
        return await prom.query_range(STATE_METRIC, start, end, step)
    except PrometheusError:
        log.debug("model timeline query failed", exc_info=True)
        raise


@dataclass
class ModelLoad:
    """One observed load: how long a model sat in `loading` before serving."""

    node: str
    router: str
    model: str
    #: When the model finished loading (first sample no longer `loading`).
    finished_at: float
    #: Point estimate. See `summarise_loads` for why it is samples x step.
    seconds: float
    #: Half-width of the uncertainty: the true value is `seconds` +/- this.
    uncertainty_s: float
    #: False when the load ended anywhere other than a serving state — a load
    #: that failed took time too, but it is not a load TIME.
    succeeded: bool

    def as_dict(self) -> dict:
        return {
            "node": self.node,
            "router": self.router,
            "model": self.model,
            "finished_at": self.finished_at,
            "seconds": self.seconds,
            "uncertainty_s": self.uncertainty_s,
            "succeeded": self.succeeded,
        }


#: States in which a model is serving, i.e. the load completed.
_LOADED_STATES = {"active", "sleeping"}


def summarise_loads(series: list, *, step_s: float) -> list[ModelLoad]:
    """Reconstruct load durations from the one-hot state series.

    HOW LONG A LOAD TOOK IS NOT DIRECTLY RECORDED. It is inferred from how many
    consecutive samples caught the model in `loading`, which makes `step_s` the
    resolution and the error bar at once.

    With samples at t0 < t1 < ... spaced `step`, a model observed `loading` for
    `m` consecutive samples between two other states started somewhere in the
    gap before the first of them and finished somewhere in the gap after the
    last. So the true duration lies in `[(m-1)*step, (m+1)*step)`, and the
    midpoint `m*step` is the honest point estimate with `step` as its error.

    This is why counting `loading` samples alone under-reports: it measures
    `(m-1)*step` at best and ignores the two half-gaps at the ends.

    **`step_s` must be near the scrape interval or this is meaningless.**
    Measured on the production cluster 2026-08-19, real loads occupy one or two
    15s samples; queried at the timeline endpoint's default 60s step, every one
    of them would collapse to a single sample and report a flat 60s. The caller
    is responsible for asking at a resolution the answer can survive.
    """
    loads: list[ModelLoad] = []

    for (node, router, model), timeline in _timelines(series).items():
        stamps = sorted(timeline)
        run = 0
        for ts in stamps:
            if timeline[ts] == "loading":
                run += 1
                continue
            if run:
                # A run just ended, at a state we can classify.
                loads.append(
                    ModelLoad(
                        node=node,
                        router=router,
                        model=model,
                        finished_at=ts,
                        seconds=run * step_s,
                        uncertainty_s=step_s,
                        succeeded=timeline[ts] in _LOADED_STATES,
                    )
                )
                run = 0
        # A run still open at the right edge is deliberately dropped: that is a
        # load IN PROGRESS, not a completed one of unknown length, and reporting
        # it would put a number that only grows beside numbers that are final.

    loads.sort(key=lambda load: load.finished_at, reverse=True)
    return loads


def latest_by_model(loads: list[ModelLoad]) -> dict[str, dict]:
    """Most recent SUCCESSFUL load per model, keyed for the Models card.

    Keyed by model name alone rather than by (node, router, model): the card
    shows a row per model per server, but the question it answers — "how long
    does this take to come up" — is about the model, and the same weights on
    two nodes load at the same speed to within this method's error bar.
    """
    out: dict[str, dict] = {}
    for load in loads:  # already newest-first
        if load.succeeded and load.model not in out:
            out[load.model] = load.as_dict()
    return out
