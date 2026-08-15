"""Threshold constants shared by the agent (which classifies) and the UI
(which color-codes).

Provenance matters here, so it's marked per-value:

  [FIELD]  taken from sparkview's field-validated anomaly logger, observed on
           real GB10 hardware. Trust these.
  [GUESS]  a plausible starting value that has NOT been validated against this
           cluster. Calibrate before relying on it for alerting.

The PSI bands are the main [GUESS]. sparkview ships
`tools/collect_psi_baseline.py` for exactly this reason — run it at idle, with
models loaded, and under inference, then set these from the observed p90/p99
rather than keeping the defaults.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- GPU clock -------------------------------------------------------------

# [FIELD] Healthy sustained load reaches ~2400MHz; degraded systems have been
# observed at 500-850MHz. Below this under load means power-delivery trouble.
CLOCK_THROTTLED_MHZ = 1400.0

# [GUESS] What counts as "under load" for the purpose of evaluating the clock.
# Below this the clock is expected to be low and is not evaluated at all.
CLOCK_LOAD_GATE_UTIL_PCT = 30.0

# [GUESS] A clock pinned suspiciously flat near a cap suggests `nvidia-smi -lgc`
# rather than a fault. Tolerance for calling a clock "externally locked".
CLOCK_LOCKED_TOLERANCE_MHZ = 15.0

# --- Temperature -----------------------------------------------------------

# [FALLBACK] Used only when the hardware won't say. Prefer the derived values
# below, which come from the silicon's own limits.
#
# CALIBRATION NOTE: the GX10 was observed at 84C during routine ComfyUI image
# generation, at 96% utilization and WITHOUT throttling. So on this hardware 80C
# is a normal working temperature, and a critical band at 80C fires constantly
# during ordinary work — which is exactly what it did.
TEMP_CRITICAL_C = 95.0
# [FALLBACK] A warning band below the fallback critical line.
TEMP_WARNING_C = 88.0

# How far below the hardware's own limit each band sits.
#
# The GPU critical band lands *on* the slowdown point: above it the hardware
# throttles itself, so it is the temperature at which you begin losing
# performance — the meaningful ceiling, distinct from the shutdown point where
# the part cuts power to survive. Warning gets 4C of lead.
GPU_WARNING_MARGIN_C = 4.0

# The CPU's only exposed trip is `critical`, where the KERNEL powers the machine
# off. Both bands are set well below it, because a cooling failure ramps fast
# and 2C of notice is no notice at all.
CPU_CRITICAL_MARGIN_C = 6.0
CPU_WARNING_MARGIN_C = 12.0


@dataclass(frozen=True)
class TempThresholds:
    """Temperature bands for one component.

    Per-component rather than shared: a GB10 GPU throttles at 86C while the CPU
    beside it is rated to 104C. One pair of numbers for both meant neither could
    be set correctly — the GPU band alarmed during normal work while the CPU
    band could not have caught a real cooling failure.
    """

    warning_c: float = TEMP_WARNING_C
    critical_c: float = TEMP_CRITICAL_C
    #: Where the numbers came from, so the UI and a reader can tell a
    #: hardware-derived band from a fallback guess.
    source: str = "fallback"

    @classmethod
    def for_gpu(cls, slowdown_c: float | None) -> TempThresholds:
        """Bands derived from NVML's slowdown threshold.

        On GB10 that is 86C (shutdown is 90C). Read from the device rather than
        hardcoded so this stays correct on different silicon, and because
        `nvidia-smi` reports these as N/A — they are only visible through NVML.
        """
        if slowdown_c is None or slowdown_c <= 0:
            return cls()
        return cls(
            warning_c=slowdown_c - GPU_WARNING_MARGIN_C,
            critical_c=slowdown_c,
            source="nvml-slowdown",
        )

    @classmethod
    def for_cpu(cls, critical_trip_c: float | None) -> TempThresholds:
        """Bands derived from the thermal zone's `critical` trip point.

        On the GX10 every acpitz zone reports 104C. psutil doesn't surface it
        for these zones, so it's read from sysfs directly.
        """
        if critical_trip_c is None or critical_trip_c <= 0:
            return cls()
        return cls(
            warning_c=critical_trip_c - CPU_WARNING_MARGIN_C,
            critical_c=critical_trip_c - CPU_CRITICAL_MARGIN_C,
            source="acpi-critical-trip",
        )


DEFAULT_TEMP_THRESHOLDS = TempThresholds()

# --- Memory ----------------------------------------------------------------

# [FIELD] sparkview triggers on >85% used *with swap active* — the combination
# matters, since 85% alone is unremarkable on a box deliberately filled with
# model weights.
MEM_HIGH_PCT = 85.0

# --- Memory pressure (PSI) -------------------------------------------------


@dataclass(frozen=True)
class PsiBands:
    """Cut points for `some avg10` / `full avg10`, as percent of time stalled.

    [GUESS] — all of it. Calibrate against a real baseline before alerting.
    `full` (every task stalled) is weighted harder than `some` (at least one).
    """

    some_mod: float = 5.0
    some_high: float = 20.0
    some_critical: float = 50.0

    full_mod: float = 1.0
    full_high: float = 10.0
    full_critical: float = 25.0


PSI_BANDS = PsiBands()
