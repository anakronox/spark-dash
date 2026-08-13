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

# [FIELD] sparkview's anomaly trigger.
#
# CALIBRATION NOTE: the GX10 was observed at 84C during routine ComfyUI image
# generation, at 96% utilization and WITHOUT throttling. So on this hardware 80C
# is a normal working temperature, not an anomaly, and alerting on it would fire
# constantly during ordinary work. These remain the defaults because they are
# the field-validated values, but they are overridable per node — see
# TEMP_WARNING_C / TEMP_CRITICAL_C in the agent settings.
TEMP_CRITICAL_C = 80.0
# [GUESS] A warning band below the field-validated critical line.
TEMP_WARNING_C = 70.0


@dataclass(frozen=True)
class TempThresholds:
    """Per-node temperature bands.

    Configurable rather than constant because what counts as "hot" depends on
    the workload mix: a node running sustained image generation legitimately
    sits where a purely-inference node would be in trouble.
    """

    warning_c: float = TEMP_WARNING_C
    critical_c: float = TEMP_CRITICAL_C


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
