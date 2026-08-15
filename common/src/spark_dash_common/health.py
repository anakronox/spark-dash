"""Roll individual signals up into a single node health state.

Kept in `common/` rather than the agent so the backend and any future consumer
classify identically — a node must never read "warning" in one view and
"critical" in another.
"""

from __future__ import annotations

from spark_dash_common.models import (
    ClockState,
    GpuMetrics,
    HealthState,
    MemoryMetrics,
    PsiMetrics,
    PsiState,
)
from spark_dash_common.thresholds import (
    DEFAULT_TEMP_THRESHOLDS,
    MEM_HIGH_PCT,
    TempThresholds,
)

_SEVERITY = {
    HealthState.GOOD: 0,
    HealthState.WARNING: 1,
    HealthState.SERIOUS: 2,
    HealthState.CRITICAL: 3,
}

_PSI_TO_HEALTH = {
    PsiState.LOW: HealthState.GOOD,
    PsiState.MOD: HealthState.WARNING,
    PsiState.HIGH: HealthState.SERIOUS,
    PsiState.CRITICAL: HealthState.CRITICAL,
}

_CLOCK_TO_HEALTH = {
    ClockState.IDLE: HealthState.GOOD,
    ClockState.PASS: HealthState.GOOD,
    ClockState.LOCKED: HealthState.SERIOUS,
    ClockState.THROTTLED: HealthState.CRITICAL,
}


def assess(
    *,
    gpu: GpuMetrics | None = None,
    memory: MemoryMetrics | None = None,
    psi: PsiMetrics | None = None,
    cpu_temp_c: float | None = None,
    temps: TempThresholds = DEFAULT_TEMP_THRESHOLDS,
    cpu_temps: TempThresholds | None = None,
) -> tuple[HealthState, list[str]]:
    """Return the worst state across all signals, plus why.

    The reasons are not decoration: the UI renders them as the text label
    beside the status color, so meaning never rides on hue alone.

    `temps` covers the GPU and `cpu_temps` the CPU. They are separate because
    the parts have very different limits — a GB10 GPU throttles at 86C while
    the CPU next to it is rated to 104C — and a single shared pair meant the
    GPU band alarmed during ordinary work while the CPU band was far too high
    to catch anything. `cpu_temps` falls back to `temps` only so existing
    callers keep working.
    """
    findings: list[tuple[HealthState, str]] = []
    cpu_bands = cpu_temps if cpu_temps is not None else temps

    if psi is not None and psi.state is not PsiState.LOW:
        findings.append((_PSI_TO_HEALTH[psi.state], f"memory pressure {psi.state.value}"))

    if gpu is not None:
        if gpu.clock_state in (ClockState.LOCKED, ClockState.THROTTLED):
            mhz = f" ({gpu.clock_mhz:.0f}MHz)" if gpu.clock_mhz is not None else ""
            findings.append(
                (_CLOCK_TO_HEALTH[gpu.clock_state], f"GPU clock {gpu.clock_state.value}{mhz}")
            )
        if gpu.temp_c is not None:
            findings.extend(_temp_findings("GPU", gpu.temp_c, temps))

    if cpu_temp_c is not None:
        findings.extend(_temp_findings("CPU", cpu_temp_c, cpu_bands))

    if memory is not None and memory.used_pct > MEM_HIGH_PCT:
        # sparkview keys on the *combination*: high usage alone is unremarkable
        # on a box deliberately full of model weights, but paired with active
        # swap it means real contention.
        if memory.swap_used_bytes > 0:
            findings.append(
                (
                    HealthState.SERIOUS,
                    f"memory {memory.used_pct:.0f}% with swap active",
                )
            )
        else:
            findings.append((HealthState.WARNING, f"memory {memory.used_pct:.0f}%"))

    if not findings:
        return HealthState.GOOD, []

    worst = max(findings, key=lambda f: _SEVERITY[f[0]])[0]
    reasons = [reason for state, reason in findings if state is worst]
    return worst, reasons


def _temp_findings(
    label: str, temp_c: float, temps: TempThresholds
) -> list[tuple[HealthState, str]]:
    if temp_c > temps.critical_c:
        return [(HealthState.CRITICAL, f"{label} {temp_c:.0f}°C")]
    if temp_c > temps.warning_c:
        return [(HealthState.WARNING, f"{label} {temp_c:.0f}°C")]
    return []
