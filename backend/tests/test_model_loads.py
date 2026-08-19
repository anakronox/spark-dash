"""Load durations reconstructed from the one-hot model state series.

Every fixture here is shaped from what the production cluster actually
recorded on 2026-08-19: real loads occupied ONE or TWO 15s samples. That is the
regime this has to be correct in — not the tidy multi-minute one it would be
easy to write tests for.
"""

from __future__ import annotations

from dataclasses import dataclass

from spark_dash_backend.timeline import latest_by_model, summarise_loads

STEP = 15.0


@dataclass
class FakeSeries:
    labels: dict
    points: list


def states(model: str, sequence: list[str], *, t0: float = 1000.0, step: float = STEP):
    """One-hot series: one per state, 1 where that state is current."""
    seen = set(sequence)
    out = []
    for state in sorted(seen):
        pts = [
            (t0 + i * step, 1.0 if s == state else 0.0) for i, s in enumerate(sequence)
        ]
        out.append(
            FakeSeries(
                labels={"node": "sparky", "router": "r", "model": model, "state": state},
                points=pts,
            )
        )
    return out


def test_a_two_sample_load_reports_two_steps():
    """The commonest real shape: qwen36-35b, 08-18 15:30:23 and 15:30:38."""
    series = states("qwen36-35b", ["sleeping", "loading", "loading", "active"])
    (load,) = summarise_loads(series, step_s=STEP)
    assert load.seconds == 30.0
    assert load.uncertainty_s == STEP
    assert load.succeeded is True


def test_a_one_sample_load_is_not_reported_as_zero():
    """qwen36-35b, 08-19 04:47:53 — a single sample caught mid-load.

    Counting samples and subtracting would call this 0s, which reads as
    "instant" when it was anything up to 30s."""
    series = states("qwen36-35b", ["unloaded", "loading", "active"])
    (load,) = summarise_loads(series, step_s=STEP)
    assert load.seconds == 15.0
    assert load.seconds - load.uncertainty_s + STEP > 0  # true value in [0, 30)


def test_a_load_that_did_not_end_in_a_serving_state_is_marked_failed():
    """A failed load took time too, but it is not a load TIME, and averaging it
    into one would understate how long the model actually takes to come up."""
    series = states("gemma4-26b", ["unloaded", "loading", "loading", "unloaded"])
    (load,) = summarise_loads(series, step_s=STEP)
    assert load.succeeded is False


def test_sleeping_counts_as_loaded():
    """A model that loads and immediately sleeps still finished loading. Its
    weights are resident; only its idle timer moved."""
    series = states("cydonia-24b", ["unloaded", "loading", "sleeping"])
    assert summarise_loads(series, step_s=STEP)[0].succeeded is True


def test_a_load_still_running_at_the_window_edge_is_dropped():
    """Reporting it would put a number that only grows beside numbers that are
    final — and it is exactly the case a reader is most likely to misread."""
    series = states("qwen36-35b", ["unloaded", "loading", "loading"])
    assert summarise_loads(series, step_s=STEP) == []


def test_multiple_loads_come_back_newest_first():
    series = states(
        "qwen36-35b",
        ["unloaded", "loading", "active", "unloaded", "loading", "loading", "active"],
    )
    loads = summarise_loads(series, step_s=STEP)
    assert [load.seconds for load in loads] == [30.0, 15.0]


def test_latest_by_model_prefers_the_newest_SUCCESSFUL_load():
    """A failure after a success must not blank the number: the last time it
    did work is still the best answer to "how long does this take"."""
    series = states(
        "qwen36-35b",
        ["unloaded", "loading", "active", "unloaded", "loading", "unloaded"],
    )
    latest = latest_by_model(summarise_loads(series, step_s=STEP))
    assert latest["qwen36-35b"]["succeeded"] is True
    assert latest["qwen36-35b"]["seconds"] == 15.0


def test_step_scales_the_answer():
    """The reason the caller must ask at scrape resolution. The same two-sample
    episode read at the timeline endpoint's default 60s step reports 120s for
    something that took about 30."""
    series = states("qwen36-35b", ["sleeping", "loading", "loading", "active"])
    assert summarise_loads(series, step_s=60.0)[0].seconds == 120.0


def test_no_series_is_empty_not_an_error():
    assert summarise_loads([], step_s=STEP) == []
    assert latest_by_model([]) == {}


def test_the_endpoint_contract_the_models_card_depends_on():
    """`loads` is keyed by model name and carries seconds + uncertainty.

    The card reads exactly these two fields and renders "~30s" with the
    uncertainty in the tooltip. Pinned here because a rename would blank the
    column silently — the card falls back to an em dash rather than erroring,
    which is right for a failed poll and wrong for a broken contract.
    """
    series = states("qwen36-35b", ["sleeping", "loading", "loading", "active"])
    latest = latest_by_model(summarise_loads(series, step_s=STEP))

    assert set(latest) == {"qwen36-35b"}
    entry = latest["qwen36-35b"]
    assert entry["seconds"] == 30.0
    assert entry["uncertainty_s"] == 15.0
    assert {"model", "seconds", "uncertainty_s", "succeeded", "finished_at"} <= set(entry)


def test_a_coarse_step_stays_honest_rather_than_wrong():
    """The estimator is sound at any resolution: a load caught in one 60s sample
    genuinely could have taken anywhere in [0, 120)s, and that is what 60 +/- 60
    says. Coarse steps lose precision, not correctness — which is why the card
    asks for 15s rather than the endpoint refusing coarser ones."""
    series = states("qwen36-35b", ["sleeping", "loading", "active"], step=60.0)
    (load,) = summarise_loads(series, step_s=60.0)
    assert load.seconds == 60.0
    assert load.uncertainty_s == 60.0
    # The true 30s load lies inside the stated interval.
    assert load.seconds - load.uncertainty_s <= 30 <= load.seconds + load.uncertainty_s
