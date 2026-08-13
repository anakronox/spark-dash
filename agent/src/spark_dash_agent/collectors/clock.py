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
    max_clock_mhz: float | None,
    *,
    absolute_floor: float = CLOCK_THROTTLED_MHZ,
    fraction_of_max: float = 0.5,
) -> float:
    """Pick the frequency below which a loaded GPU counts as throttled.

    sparkview's 1400MHz was derived on GB10 specifically. Deriving it from the
    hardware's own maximum instead makes it self-calibrating — the GX10 reports
    max_sm_clock 3003MHz, which puts the threshold at ~1500MHz, close to the
    field-validated value while also being correct on different hardware.

    The absolute floor is kept as a lower bound so a GPU reporting an
    implausibly low maximum can't produce a threshold near zero that would
    never fire.
    """
    if not max_clock_mhz or max_clock_mhz <= 0:
        return absolute_floor
    return max(absolute_floor, max_clock_mhz * fraction_of_max)


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
