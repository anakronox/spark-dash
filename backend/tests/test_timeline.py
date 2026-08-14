"""Model lifecycle reconstruction.

Built from Prometheus's existing one-hot state metric rather than a new store,
so history reaches back as far as retention rather than starting when the
feature shipped.
"""

from dataclasses import dataclass

from spark_dash_backend.timeline import TRANSITIONS, extract_events


@dataclass
class FakeSeries:
    labels: dict
    points: list


def state_series(node, router, model, state, samples):
    """One-hot series: `samples` is [(ts, 1|0), ...] for this state."""
    return FakeSeries(
        labels={"node": node, "router": router, "model": model, "state": state},
        points=samples,
    )


def test_detects_a_sleep_transition():
    """The common case: a model idles past --sleep-idle-seconds."""
    series = [
        state_series("sparky", "r1", "qwen", "active", [(100, 1), (200, 1), (300, 0)]),
        state_series("sparky", "r1", "qwen", "sleeping", [(100, 0), (200, 0), (300, 1)]),
    ]
    events = extract_events(series)

    assert len(events) == 1
    assert events[0].from_state == "active"
    assert events[0].to_state == "sleeping"
    assert events[0].label == "slept"
    assert events[0].ts == 300


def test_eviction_is_marked_cold():
    """Evicted means the next request waits for weights to load — the whole
    reason this view exists."""
    series = [
        state_series("sparky", "r1", "qwen", "active", [(100, 1), (200, 0)]),
        state_series("sparky", "r1", "qwen", "unloaded", [(100, 0), (200, 1)]),
    ]
    events = extract_events(series)

    assert events[0].label == "evicted"
    assert events[0].cold is True


def test_waking_is_not_cold():
    """A sleeping model still has its process; waking is cheap relative to a
    cold load, so it shouldn't be counted as one."""
    series = [
        state_series("sparky", "r1", "qwen", "sleeping", [(100, 1), (200, 0)]),
        state_series("sparky", "r1", "qwen", "active", [(100, 0), (200, 1)]),
    ]
    events = extract_events(series)

    assert events[0].label == "woke"
    assert events[0].cold is False


def test_first_sample_is_not_reported_as_a_transition():
    """Otherwise every model appears to have just loaded at the left edge of
    the window, which is an artifact of where the window starts."""
    series = [
        state_series("sparky", "r1", "qwen", "active", [(100, 1), (200, 1)]),
    ]
    assert extract_events(series) == []


def test_steady_state_produces_no_events():
    series = [
        state_series("sparky", "r1", "qwen", "sleeping", [(t, 1) for t in range(0, 600, 60)]),
    ]
    assert extract_events(series) == []


def test_multiple_models_are_tracked_independently():
    series = [
        state_series("sparky", "r1", "a", "active", [(100, 1), (200, 0)]),
        state_series("sparky", "r1", "a", "sleeping", [(100, 0), (200, 1)]),
        state_series("sparky", "r1", "b", "sleeping", [(100, 1), (200, 1)]),
    ]
    events = extract_events(series)

    assert len(events) == 1
    assert events[0].model == "a"


def test_same_model_on_two_routers_is_not_conflated():
    """A model name can be registered with more than one router on a node."""
    series = [
        state_series("sparky", "r1", "shared", "active", [(100, 1), (200, 0)]),
        state_series("sparky", "r1", "shared", "sleeping", [(100, 0), (200, 1)]),
        state_series("sparky", "r2", "shared", "active", [(100, 1), (200, 1)]),
    ]
    events = extract_events(series)

    assert len(events) == 1
    assert events[0].router == "r1"


def test_events_are_newest_first():
    series = [
        state_series("n", "r", "m", "active", [(100, 1), (200, 0), (300, 1)]),
        state_series("n", "r", "m", "sleeping", [(100, 0), (200, 1), (300, 0)]),
    ]
    events = extract_events(series)

    assert [e.ts for e in events] == [300, 200]


def test_unrecognised_transition_is_described_not_dropped():
    """An unexpected transition is more interesting than a familiar one, so it
    must not vanish for lack of a friendly name."""
    series = [
        state_series("n", "r", "m", "unloaded", [(100, 1), (200, 0)]),
        state_series("n", "r", "m", "sleeping", [(100, 0), (200, 1)]),
    ]
    events = extract_events(series)

    assert len(events) == 1
    assert ("unloaded", "sleeping") not in TRANSITIONS
    assert events[0].label == "unloaded → sleeping"


def test_full_lifecycle():
    """unloaded -> loading -> active -> sleeping, as a router actually behaves."""
    ts = [100, 200, 300, 400]
    series = [
        state_series("n", "r", "m", "unloaded", [(100, 1), (200, 0), (300, 0), (400, 0)]),
        state_series("n", "r", "m", "loading", [(100, 0), (200, 1), (300, 0), (400, 0)]),
        state_series("n", "r", "m", "active", [(100, 0), (200, 0), (300, 1), (400, 0)]),
        state_series("n", "r", "m", "sleeping", [(100, 0), (200, 0), (300, 0), (400, 1)]),
    ]
    events = extract_events(series)
    assert [e.label for e in reversed(events)] == ["loading", "loaded", "slept"]
    assert [e.ts for e in reversed(events)] == ts[1:]


def test_series_missing_a_state_label_is_ignored():
    """Guards against a malformed series taking the whole view down."""
    series = [FakeSeries(labels={"node": "n", "model": "m"}, points=[(100, 1)])]
    assert extract_events(series) == []


def test_no_series_at_all():
    assert extract_events([]) == []
