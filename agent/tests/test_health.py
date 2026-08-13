"""Health rollup lives in common/ so agent and backend classify identically —
a node must never read 'warning' in one view and 'critical' in another.
"""

from spark_dash_common.health import assess
from spark_dash_common.models import (
    ClockState,
    GpuMetrics,
    HealthState,
    MemoryMetrics,
    PsiMetrics,
    PsiState,
)


def _mem(used_pct: float, swap: int = 0) -> MemoryMetrics:
    total = 1000
    used = int(total * used_pct / 100)
    return MemoryMetrics(
        total_bytes=total,
        available_bytes=total - used,
        used_bytes=used,
        swap_used_bytes=swap,
    )


def test_no_signals_is_good():
    state, reasons = assess()
    assert state is HealthState.GOOD
    assert reasons == []


def test_healthy_node_is_good():
    state, _ = assess(
        gpu=GpuMetrics(util_pct=80, temp_c=65, clock_state=ClockState.PASS),
        memory=_mem(50),
        psi=PsiMetrics(state=PsiState.LOW),
        cpu_temp_c=55,
    )
    assert state is HealthState.GOOD


def test_throttled_clock_is_critical():
    state, reasons = assess(
        gpu=GpuMetrics(util_pct=95, clock_mhz=700, clock_state=ClockState.THROTTLED)
    )
    assert state is HealthState.CRITICAL
    assert any("THROTTLED" in r for r in reasons)


def test_locked_clock_is_serious_not_critical():
    """An operator-set cap is deliberate — worth surfacing, not an emergency."""
    state, _ = assess(gpu=GpuMetrics(util_pct=95, clock_mhz=1000, clock_state=ClockState.LOCKED))
    assert state is HealthState.SERIOUS


def test_temperature_bands():
    assert assess(gpu=GpuMetrics(util_pct=1, temp_c=65))[0] is HealthState.GOOD
    assert assess(gpu=GpuMetrics(util_pct=1, temp_c=75))[0] is HealthState.WARNING
    assert assess(gpu=GpuMetrics(util_pct=1, temp_c=85))[0] is HealthState.CRITICAL


def test_cpu_temperature_counts_too():
    state, reasons = assess(cpu_temp_c=85)
    assert state is HealthState.CRITICAL
    assert any("CPU" in r for r in reasons)


def test_high_memory_alone_is_only_a_warning():
    """A box deliberately full of model weights is not an incident."""
    state, _ = assess(memory=_mem(90))
    assert state is HealthState.WARNING


def test_high_memory_with_swap_is_serious():
    """The combination is what sparkview keys on — real contention."""
    state, reasons = assess(memory=_mem(90, swap=1))
    assert state is HealthState.SERIOUS
    assert any("swap" in r for r in reasons)


def test_psi_maps_through_to_health():
    assert assess(psi=PsiMetrics(state=PsiState.LOW))[0] is HealthState.GOOD
    assert assess(psi=PsiMetrics(state=PsiState.MOD))[0] is HealthState.WARNING
    assert assess(psi=PsiMetrics(state=PsiState.HIGH))[0] is HealthState.SERIOUS
    assert assess(psi=PsiMetrics(state=PsiState.CRITICAL))[0] is HealthState.CRITICAL


def test_worst_signal_wins():
    state, _ = assess(
        gpu=GpuMetrics(util_pct=95, clock_mhz=700, clock_state=ClockState.THROTTLED),
        memory=_mem(90),
        psi=PsiMetrics(state=PsiState.MOD),
    )
    assert state is HealthState.CRITICAL


def test_reasons_only_describe_the_winning_severity():
    """Reasons become the visible label beside the status color, so they must
    explain the state actually shown — not every lesser gripe."""
    state, reasons = assess(
        gpu=GpuMetrics(util_pct=95, temp_c=85, clock_state=ClockState.PASS),
        memory=_mem(90),
    )
    assert state is HealthState.CRITICAL
    assert len(reasons) == 1
    assert "85" in reasons[0]


def test_idle_clock_is_not_a_fault():
    state, _ = assess(gpu=GpuMetrics(util_pct=0, clock_mhz=300, clock_state=ClockState.IDLE))
    assert state is HealthState.GOOD
