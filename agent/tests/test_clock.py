"""The clock state machine is the piece most likely to produce false alarms,
so the load-gating and ramp-up behavior are covered explicitly.
"""

from spark_dash_agent.collectors.clock import ClockSignals, ClockTracker, classify_clock
from spark_dash_common.models import ClockState


def test_idle_when_not_under_load():
    """A low clock at idle is correct power management, not a fault."""
    state = classify_clock(ClockSignals(util_pct=2.0, clock_mhz=300.0))
    assert state is ClockState.IDLE


def test_pass_when_loaded_and_clock_healthy():
    state = classify_clock(ClockSignals(util_pct=95.0, clock_mhz=2400.0))
    assert state is ClockState.PASS


def test_throttled_when_loaded_and_clock_low():
    state = classify_clock(ClockSignals(util_pct=95.0, clock_mhz=700.0))
    assert state is ClockState.THROTTLED


def test_locked_beats_frequency_check():
    """An operator-set cap is deliberate, so it must not read as a fault."""
    state = classify_clock(
        ClockSignals(util_pct=95.0, clock_mhz=700.0, locked_by_setting=True)
    )
    assert state is ClockState.LOCKED


def test_hardware_throttle_flag_wins_over_healthy_frequency():
    """NVML knows about throttling the raw frequency threshold would miss."""
    state = classify_clock(
        ClockSignals(util_pct=95.0, clock_mhz=2400.0, hw_throttled=True)
    )
    assert state is ClockState.THROTTLED


def test_unknown_clock_is_idle_not_throttled():
    """Missing data must never be reported as a fault."""
    state = classify_clock(ClockSignals(util_pct=95.0, clock_mhz=None))
    assert state is ClockState.IDLE


def test_boundary_exactly_at_threshold_passes():
    assert classify_clock(ClockSignals(util_pct=95.0, clock_mhz=1400.0)) is ClockState.PASS
    assert classify_clock(ClockSignals(util_pct=95.0, clock_mhz=1399.9)) is ClockState.THROTTLED


def test_tracker_requires_sustained_load_before_judging():
    """Clocks lag utilization on ramp-up; judging immediately would false-alarm."""
    tracker = ClockTracker(sustained_samples=3)
    low_clock_under_load = ClockSignals(util_pct=99.0, clock_mhz=600.0)

    assert tracker.update(low_clock_under_load) is ClockState.IDLE
    assert tracker.update(low_clock_under_load) is ClockState.IDLE
    # Third consecutive loaded sample: now it counts.
    assert tracker.update(low_clock_under_load) is ClockState.THROTTLED


def test_tracker_resets_when_load_drops():
    tracker = ClockTracker(sustained_samples=2)
    loaded = ClockSignals(util_pct=99.0, clock_mhz=600.0)
    idle = ClockSignals(util_pct=1.0, clock_mhz=300.0)

    tracker.update(loaded)
    assert tracker.update(idle) is ClockState.IDLE
    # Counter restarted, so one loaded sample isn't enough again.
    assert tracker.update(loaded) is ClockState.IDLE
    assert tracker.update(loaded) is ClockState.THROTTLED


def test_tracker_reports_pass_on_healthy_sustained_load():
    tracker = ClockTracker(sustained_samples=2)
    healthy = ClockSignals(util_pct=99.0, clock_mhz=2400.0)
    tracker.update(healthy)
    assert tracker.update(healthy) is ClockState.PASS
