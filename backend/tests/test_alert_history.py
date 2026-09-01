"""Alert episode reconstruction from Prometheus's ALERTS series.

The traps here are all about a series that exists only while an alert is
active: absence means either "resolved" or "Prometheus wasn't running", and
pending/firing are two states of one episode rather than two events.
"""

from dataclasses import dataclass

from spark_dash_backend.alert_history import (
    DEFAULT_GAP_TOLERANCE_S,
    extract_episodes,
    summarise,
)


@dataclass
class FakeSeries:
    labels: dict
    points: list


def alerts_series(alertname, alertstate, samples, *, severity="warning", node="sparky"):
    """ALERTS carries 1 while active and is ABSENT otherwise — never 0."""
    return FakeSeries(
        labels={
            "__name__": "ALERTS",
            "alertname": alertname,
            "alertstate": alertstate,
            "severity": severity,
            "node": node,
        },
        points=[(ts, 1.0) for ts in samples],
    )


def run(ts0, count, step=60):
    return [ts0 + i * step for i in range(count)]


class TestEpisodes:
    def test_a_continuous_run_is_one_episode(self):
        series = [alerts_series("GpuTemperatureHigh", "pending", run(1000, 10))]
        episodes = extract_episodes(series, window_end=1000 + 9 * 60)
        assert len(episodes) == 1
        assert episodes[0].alertname == "GpuTemperatureHigh"
        assert episodes[0].duration_s == 9 * 60

    def test_pending_then_firing_is_ONE_episode(self):
        """An alert crosses from pending to firing when its `for:` elapses.
        Reporting that as two rows would double-count every alert that ever
        fired, and would make the duration meaningless."""
        series = [
            alerts_series("MemoryPressureCritical", "pending", run(1000, 3)),
            alerts_series("MemoryPressureCritical", "firing", run(1180, 5)),
        ]
        episodes = extract_episodes(series, window_end=1180 + 4 * 60)
        assert len(episodes) == 1
        e = episodes[0]
        assert e.fired is True
        assert e.fired_at == 1180
        # Measured from when it went pending, not from when it fired.
        assert e.started_at == 1000

    def test_pending_that_never_fires_is_kept(self):
        """The state that mattered most when this was written: every ALERTS
        series in the previous week was pending and nothing had ever fired,
        because several rules had `for:` windows longer than their events. An
        implementation that filtered pending out would have shown an empty
        page while that was true."""
        series = [alerts_series("MemoryPressureHigh", "pending", run(1000, 3))]
        episodes = extract_episodes(series, window_end=1000 + 2 * 60)
        assert len(episodes) == 1
        assert episodes[0].fired is False
        assert episodes[0].fired_at is None

    def test_a_real_gap_splits_episodes(self):
        """Resolved, then fired again later — two separate incidents."""
        series = [
            alerts_series("NodeAgentDown", "firing", run(1000, 3)),
            alerts_series("NodeAgentDown", "firing", run(50_000, 3)),
        ]
        episodes = extract_episodes(series, window_end=50_000 + 2 * 60)
        assert len(episodes) == 2

    def test_a_scrape_sized_gap_does_not_split(self):
        """Prometheus restarting mid-alert leaves a hole in ALERTS and the
        alert re-enters pending afterwards — observed repeatedly during the
        2026-08-16 deploys. Without tolerance, one incident reports as several,
        which would badly overstate how often things go wrong."""
        before = run(1000, 3)
        after = run(1000 + 2 * 60 + int(DEFAULT_GAP_TOLERANCE_S) - 30, 3)
        series = [alerts_series("NodeAgentDown", "firing", before + after)]
        episodes = extract_episodes(series, window_end=after[-1])
        assert len(episodes) == 1

    def test_ongoing_when_the_last_sample_is_recent(self):
        samples = run(1000, 5)
        episodes = extract_episodes(
            series=[alerts_series("X", "firing", samples)], window_end=samples[-1] + 30
        )
        assert episodes[0].ongoing is True

    def test_not_ongoing_when_it_stopped_long_ago(self):
        samples = run(1000, 5)
        episodes = extract_episodes(
            series=[alerts_series("X", "firing", samples)], window_end=samples[-1] + 10_000
        )
        assert episodes[0].ongoing is False

    def test_different_nodes_are_different_episodes(self):
        """Same rule, two nodes, is two incidents — otherwise a cluster-wide
        problem collapses into one row and hides its own scope."""
        series = [
            alerts_series("GpuTemperatureHigh", "pending", run(1000, 3), node="sparky"),
            alerts_series("GpuTemperatureHigh", "pending", run(1000, 3), node="spark2"),
        ]
        episodes = extract_episodes(series, window_end=1000 + 2 * 60)
        assert len(episodes) == 2
        assert {e.node for e in episodes} == {"sparky", "spark2"}

    def test_newest_first(self):
        series = [
            alerts_series("A", "firing", run(1000, 2)),
            alerts_series("B", "firing", run(90_000, 2)),
        ]
        episodes = extract_episodes(series, window_end=90_060)
        assert [e.alertname for e in episodes] == ["B", "A"]

    def test_no_alerts_is_empty_not_an_error(self):
        assert extract_episodes([], window_end=1000) == []


class TestSummary:
    def test_counts_split_fired_from_pending_only(self):
        series = [
            alerts_series("A", "pending", run(1000, 2)),
            alerts_series("B", "pending", run(1000, 2)),
            alerts_series("B", "firing", run(1120, 2)),
        ]
        s = summarise(extract_episodes(series, window_end=1240))
        assert s["episodes"] == 2
        assert s["fired"] == 1
        # The number that says a rule is mistuned rather than its condition rare.
        assert s["pending_only"] == 1

    def test_empty(self):
        assert summarise([]) == {
            "episodes": 0,
            "fired": 0,
            "pending_only": 0,
            "ongoing": 0,
            "during_maintenance": 0,
        }


def test_gap_tolerance_scales_with_the_step():
    """A gap cannot be smaller than the sampling resolution.

    A fixed 150s tolerance meant that at any step above it EVERY sample looked
    like a new episode. Found the first time a 7-day window was queried at its
    natural 3600s step: one continuous alert came back as 21 episodes — exactly
    one per sample — and drew 21 marks on the charts where there was one
    incident.
    """
    from spark_dash_backend.prometheus import step_seconds

    assert step_seconds("60s") == 60
    assert step_seconds("3600s") == 3600
    assert step_seconds("5m") == 300
    assert step_seconds("1h") == 3600
    assert step_seconds("") == 60


def test_a_continuous_alert_is_one_episode_at_a_coarse_step():
    """The regression itself, at the resolution that exposed it."""
    step_s = 3600
    start = 1_700_000_000
    # One alert, firing continuously across six hourly samples.
    series = [
        alerts_series(
            "InferenceTargetScrapeFailing", "firing", run(start, 6, step=step_s)
        )
    ]

    fragmented = extract_episodes(
        series, window_end=start + 6 * step_s, gap_tolerance_s=150.0
    )
    merged = extract_episodes(
        series, window_end=start + 6 * step_s, gap_tolerance_s=2.5 * step_s
    )

    # What was happening: one episode per sample.
    assert len(fragmented) == 6
    # What should happen.
    assert len(merged) == 1
