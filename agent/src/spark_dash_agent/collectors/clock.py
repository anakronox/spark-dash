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
    the instant work arrives, but clocks take a moment to boost. Demanding a few
    consecutive loaded samples costs a second or two of detection latency and
    removes that whole class of false positive.
    """

    def __init__(
        self,
        sustained_samples: int = 3,
        load_gate_pct: float = CLOCK_LOAD_GATE_UTIL_PCT,
    ) -> None:
        self._sustained_samples = sustained_samples
        self._load_gate_pct = load_gate_pct
        self._consecutive_loaded = 0

    def update(self, signals: ClockSignals) -> ClockState:
        if signals.util_pct >= self._load_gate_pct:
            self._consecutive_loaded += 1
        else:
            self._consecutive_loaded = 0

        under_load = self._consecutive_loaded >= self._sustained_samples
        return classify_clock(signals, under_load=under_load, load_gate_pct=self._load_gate_pct)
