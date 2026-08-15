"""The clock state machine is the piece most likely to produce false alarms,
so the load-gating and ramp-up behavior are covered explicitly.
"""

from spark_dash_agent.collectors.clock import (
    ClockSignals,
    ClockTracker,
    classify_clock,
    throttle_threshold_mhz,
)
from spark_dash_common.models import ClockState

# Measured on the GX10, 2026-08-16. The gap between these two is the whole
# point: the boost ceiling is never approached, the applications clock is what
# the observed 2359-2483MHz range brackets.
GB10_MAX_SM_CLOCK = 3003.0
GB10_APPLICATIONS_CLOCK = 2418.0


class TestThrottleThreshold:
    def test_derived_from_the_applications_clock(self):
        """2418 * 0.5 = 1209, so the field-validated floor applies. The point
        isn't the number — it's that the reference is a clock the GPU actually
        targets rather than a ceiling it never reaches."""
        assert throttle_threshold_mhz(GB10_APPLICATIONS_CLOCK) == 1400.0

    def test_the_boost_ceiling_would_have_given_a_different_answer(self):
        """Deriving from max_sm_clock produced 1502MHz. It landed near the
        field-validated 1400 by luck, not calibration: the arithmetic was
        measuring against a speed this part never reaches."""
        assert throttle_threshold_mhz(GB10_MAX_SM_CLOCK) == 1501.5

    def test_floor_applies_when_the_target_is_implausibly_low(self):
        """A GPU reporting a tiny target must not yield a threshold near zero
        that could never fire."""
        assert throttle_threshold_mhz(100.0) == 1400.0

    def test_unreadable_target_falls_back_to_the_field_value(self):
        assert throttle_threshold_mhz(None) == 1400.0
        assert throttle_threshold_mhz(0) == 1400.0

    def test_a_high_target_scales_above_the_floor(self):
        """On hardware whose target is genuinely high, the fraction should win
        over the floor — that's what makes this self-calibrating rather than a
        constant with extra steps."""
        assert throttle_threshold_mhz(4000.0) == 2000.0

    def test_healthy_gb10_clock_is_far_above_the_threshold(self):
        """The observed floor across three days was 2359MHz, including at idle.
        Whatever the threshold is, normal operation must not approach it."""
        assert throttle_threshold_mhz(GB10_APPLICATIONS_CLOCK) * 1.5 < 2359.0


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
    tracker = ClockTracker(sustained_seconds=5.0)
    low_clock_under_load = ClockSignals(util_pct=99.0, clock_mhz=600.0)

    assert tracker.update(low_clock_under_load, now=0.0) is ClockState.IDLE
    assert tracker.update(low_clock_under_load, now=4.9) is ClockState.IDLE
    # Load has now persisted long enough to judge.
    assert tracker.update(low_clock_under_load, now=5.0) is ClockState.THROTTLED


def test_tracker_is_independent_of_polling_rate():
    """The bug this replaced: counting samples made "sustained" mean 3 seconds
    at a 1s live-view cadence but 45 at a 15s Prometheus scrape — and never
    conclude at all under sparse polling."""
    loaded = ClockSignals(util_pct=99.0, clock_mhz=600.0)

    sparse = ClockTracker(sustained_seconds=5.0)
    sparse.update(loaded, now=0.0)
    # Only a second sample, but 60s of wall clock have passed.
    assert sparse.update(loaded, now=60.0) is ClockState.THROTTLED

    rapid = ClockTracker(sustained_seconds=5.0)
    for i in range(20):
        state = rapid.update(loaded, now=i * 0.1)
    # Twenty samples but only 1.9s elapsed — not yet sustained.
    assert state is ClockState.IDLE


def test_tracker_resets_when_load_drops():
    tracker = ClockTracker(sustained_seconds=5.0)
    loaded = ClockSignals(util_pct=99.0, clock_mhz=600.0)
    idle = ClockSignals(util_pct=1.0, clock_mhz=300.0)

    tracker.update(loaded, now=0.0)
    assert tracker.update(idle, now=4.0) is ClockState.IDLE
    # Timer restarted, so the earlier busy period doesn't count toward the new one.
    assert tracker.update(loaded, now=6.0) is ClockState.IDLE
    assert tracker.update(loaded, now=11.0) is ClockState.THROTTLED


def test_tracker_reports_pass_on_healthy_sustained_load():
    tracker = ClockTracker(sustained_seconds=5.0)
    healthy = ClockSignals(util_pct=99.0, clock_mhz=2400.0)
    tracker.update(healthy, now=0.0)
    assert tracker.update(healthy, now=6.0) is ClockState.PASS
