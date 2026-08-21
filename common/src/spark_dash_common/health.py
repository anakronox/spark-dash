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
    MEM_UNEXPLAINED_PCT,
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
    model_bytes: int = 0,
) -> tuple[HealthState, list[str]]:
    """Return the worst state across all signals, plus why.

    The reasons are not decoration: the UI renders them as the text label
    beside the status color, so meaning never rides on hue alone.

    `model_bytes` is GPU memory held by LLM runtimes — what the node is
    SUPPOSED to be full of. Passed in rather than derived here because the
    process list lives in the snapshot builder, and health has never taken one.
    Zero is a safe default: it degrades this check to the old "how full is it"
    question rather than silently passing.

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

    model_pct = (
        100.0 * model_bytes / memory.total_bytes
        if memory is not None and memory.total_bytes and model_bytes
        else 0.0
    )
    if memory is not None and memory.used_pct > MEM_HIGH_PCT:
        # HOW FULL IS NOT THE QUESTION; WHETHER IT IS EXPLAINED IS.
        #
        # This rule used to escalate to SERIOUS whenever usage was high AND
        # swap was in use, on the theory that the combination meant real
        # contention. `alerts.yml` records the measurement that killed that
        # theory for the equivalent alert, and it applies here identically:
        # swap_used is a LEVEL, not a flow. Pages evicted during some past
        # squeeze sit there indefinitely because Linux never faults them back
        # proactively, so the conjunct is ~always true and the escalation was
        # automatic. Observed 2026-08-21: every node in the cluster carried
        # 1.4-2.5 GiB of parked swap, and both cluster members read SERIOUS
        # permanently while doing exactly what they were built for.
        #
        # So the same answer Z3 gave the alert: subtract what resident model
        # weights explain. A node full of weights is not unhealthy — it is
        # loaded. A node full of something nobody can name is worth a look.
        # Pressure, which is the "real contention" the old rule was reaching
        # for, is already a separate finding above and reads PSI, which IS a
        # flow.
        unexplained = memory.used_pct - model_pct
        if unexplained > MEM_UNEXPLAINED_PCT:
            findings.append(
                (
                    HealthState.WARNING,
                    f"memory {memory.used_pct:.0f}%, "
                    f"{unexplained:.0f}% not model weights",
                )
            )

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
