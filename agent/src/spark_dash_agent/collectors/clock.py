"""GPU clock health as a load-gated state machine.

Why gated: a low clock at idle is correct power management, not a fault. Only a
low clock *while the GPU is actually working* indicates a problem — on GB10
typically power delivery. Evaluating ungated would fire constantly on an idle
node.

Two signals feed this, in order of preference:

1. NVML throttle reasons, when the driver exposes them — precise about *why*
   the clock is where it is (an applied clock setting vs. hardware slowdown).
2. A raw frequency threshold — sparkview's field-derived fallback, which works
   regardless of NVML support. GB10 support for throttle reasons is uncertain,
   so the fallback is not optional.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from spark_dash_common.models import ClockState
from spark_dash_common.thresholds import (
    CLOCK_LOAD_GATE_UTIL_PCT,
    CLOCK_THROTTLED_MHZ,
)


@dataclass(frozen=True)
class ClockSignals:
    """What we know about the clock this tick.

    The two booleans are tri-state on purpose: `None` means "the driver didn't
    tell us", which is different from "it told us no".
    """

    util_pct: float
    clock_mhz: float | None
    locked_by_setting: bool | None = None
    hw_throttled: bool | None = None


def throttle_threshold_mhz(
    target_clock_mhz: float | None,
    *,
    absolute_floor: float = CLOCK_THROTTLED_MHZ,
    fraction_of_target: float = 0.5,
) -> float:
    """Pick the frequency below which a loaded GPU counts as throttled.

    DERIVED FROM THE APPLICATIONS CLOCK, NOT max_sm_clock. The GX10 reports
    max_sm_clock 3003MHz, but measured over three days it never went near it:
    the clock sat between 2359 and 2483MHz, averaging 2406, and did not drop at
    idle either. `nvmlDeviceGetApplicationsClock` reports 2418MHz, which is what
    the GPU actually targets for compute and which the observed range brackets
    almost exactly.

    So max_sm_clock is a boost ceiling this part never reaches, and deriving a
    threshold from it measured against a speed that doesn't exist. That it
    landed near the field-validated 1400MHz was luck, not calibration — on a
    part whose boost ceiling sits further from its applications clock, the same
    arithmetic would have produced a threshold well off.

    The absolute floor is kept as a lower bound so a GPU reporting an
    implausibly low target can't produce a threshold near zero that would never
    fire. On GB10 the floor is what applies: 2418 * 0.5 = 1209, below the
    field-validated 1400.
    """
    if not target_clock_mhz or target_clock_mhz <= 0:
        return absolute_floor
    return max(absolute_floor, target_clock_mhz * fraction_of_target)


def classify_clock(
    signals: ClockSignals,
    *,
    under_load: bool | None = None,
    throttled_mhz: float = CLOCK_THROTTLED_MHZ,
    load_gate_pct: float = CLOCK_LOAD_GATE_UTIL_PCT,
) -> ClockState:
    """Map clock signals onto a state.

    `under_load` overrides the instantaneous utilization gate, letting the
    caller require *sustained* load (see `ClockTracker`). Pass `None` to gate on
    this tick's utilization alone.
    """
    if signals.clock_mhz is None:
        return ClockState.IDLE

    loaded = (signals.util_pct >= load_gate_pct) if under_load is None else under_load
    if not loaded:
        return ClockState.IDLE

    # An explicit clock cap is a deliberate operator action, not a fault — so
    # it's reported distinctly rather than as a throttle.
    if signals.locked_by_setting:
        return ClockState.LOCKED

    if signals.hw_throttled:
        return ClockState.THROTTLED

    if signals.clock_mhz < throttled_mhz:
        return ClockState.THROTTLED

    return ClockState.PASS


class ClockTracker:
    """Requires load to persist before judging the clock.

    Without this, every ramp-up would briefly look THROTTLED: utilization jumps
    the instant work arrives, but clocks take a moment to boost. A short
    settling period removes that whole class of false positive.

    Measured in SECONDS, not samples. Snapshots are built on demand, so the
    sampling rate varies wildly — ~1-2s when the dashboard is open, 15s from a
    Prometheus scrape, and arbitrarily sparse when only something like a manual
    curl is polling. Counting samples would make "sustained" mean 3 seconds in
    one case and 45 in another, and would never conclude at all under sparse
    polling. Wall-clock duration means the same thing regardless of who's
    asking.
    """

    def __init__(
        self,
        sustained_seconds: float = 5.0,
        load_gate_pct: float = CLOCK_LOAD_GATE_UTIL_PCT,
        throttled_mhz: float = CLOCK_THROTTLED_MHZ,
    ) -> None:
        self._sustained_seconds = sustained_seconds
        self._load_gate_pct = load_gate_pct
        self._throttled_mhz = throttled_mhz
        self._loaded_since: float | None = None

    def update(self, signals: ClockSignals, now: float | None = None) -> ClockState:
        now = time.monotonic() if now is None else now

        if signals.util_pct >= self._load_gate_pct:
            if self._loaded_since is None:
                self._loaded_since = now
        else:
            # Any dip below the gate restarts the clock — a brief idle moment
            # means the GPU had a chance to settle, so the next busy period is
            # a fresh ramp-up.
            self._loaded_since = None

        under_load = (
            self._loaded_since is not None and (now - self._loaded_since) >= self._sustained_seconds
        )
        return classify_clock(
            signals,
            under_load=under_load,
            load_gate_pct=self._load_gate_pct,
            throttled_mhz=self._throttled_mhz,
        )
