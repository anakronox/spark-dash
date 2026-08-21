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
from spark_dash_common.thresholds import TempThresholds

# Explicit rather than relying on the defaults: these tests are about the
# banding logic, and shouldn't fail because a fallback constant was retuned.
GPU_BANDS = TempThresholds(warning_c=70, critical_c=80)
CPU_BANDS = TempThresholds(warning_c=92, critical_c=98)


def _mem(used_pct: float, swap: int = 0, total: int = 1000) -> MemoryMetrics:
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
    def at(temp):
        return assess(gpu=GpuMetrics(util_pct=1, temp_c=temp), temps=GPU_BANDS)[0]

    assert at(65) is HealthState.GOOD
    assert at(75) is HealthState.WARNING
    assert at(85) is HealthState.CRITICAL


def test_cpu_temperature_counts_too():
    state, reasons = assess(cpu_temp_c=99, temps=GPU_BANDS, cpu_temps=CPU_BANDS)
    assert state is HealthState.CRITICAL
    assert any("CPU" in r for r in reasons)


def test_cpu_and_gpu_are_judged_against_their_own_bands():
    """The bug this fixes: one shared TempThresholds pair judged both parts. A
    GB10 GPU throttles at 86C while the CPU beside it is rated to 104C, so a
    single pair could not be right for both — in practice the GPU band alarmed
    during ordinary work and the CPU band was far too low to mean anything.

    85C is critical for this GPU and unremarkable for this CPU.
    """
    gpu_hot, gpu_reasons = assess(
        gpu=GpuMetrics(util_pct=95, temp_c=85), temps=GPU_BANDS, cpu_temps=CPU_BANDS
    )
    assert gpu_hot is HealthState.CRITICAL
    assert any("GPU" in r for r in gpu_reasons)

    cpu_fine, _ = assess(cpu_temp_c=85, temps=GPU_BANDS, cpu_temps=CPU_BANDS)
    assert cpu_fine is HealthState.GOOD


def test_cpu_bands_fall_back_to_the_gpu_pair_when_unset():
    """Only so existing callers keep working; the agent always passes both."""
    state, _ = assess(cpu_temp_c=85, temps=GPU_BANDS)
    assert state is HealthState.CRITICAL


def test_a_node_full_of_model_weights_is_healthy():
    """The whole point. A node holding a large model is doing its job, not
    failing at it — and it stays that way indefinitely, which is what made the
    old rule impossible to clear. Observed 2026-08-21: both cluster members sat
    at 87-88% with a 96.8 GiB shard each and read SERIOUS permanently."""
    state, reasons = assess(memory=_mem(90, total=100), model_bytes=88)
    assert state is HealthState.GOOD, reasons


def test_memory_nobody_can_account_for_is_a_warning():
    """Same level, different cause: 90% used with almost none of it explained
    by resident weights is worth a look, whether or not models are loaded."""
    state, reasons = assess(memory=_mem(90, total=100), model_bytes=10)
    assert state is HealthState.WARNING
    assert any("not model weights" in r for r in reasons)


def test_swap_no_longer_escalates_anything():
    """`swap_used` is a LEVEL, not a flow. Pages evicted during some past
    squeeze sit there indefinitely because Linux never faults them back, so the
    old "high memory AND swap in use" conjunct was ~always true — every node in
    this cluster carries 1.4-2.5 GiB of parked swap. alerts.yml dropped the same
    conjunct for the same measured reason; this rule kept it for two days
    longer and pinned both cluster nodes to SERIOUS.

    Real contention is PSI's job, and PSI is a flow."""
    loaded = assess(memory=_mem(90, total=100, swap=8), model_bytes=88)
    assert loaded[0] is HealthState.GOOD, loaded[1]
    assert not any("swap" in r for r in loaded[1])


def test_without_process_data_it_falls_back_to_how_full():
    """`model_bytes` defaults to 0, so a caller that cannot supply the process
    list degrades to the old question rather than silently passing everything."""
    state, _ = assess(memory=_mem(90, total=100))
    assert state is HealthState.WARNING


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
        temps=GPU_BANDS,
    )
    assert state is HealthState.CRITICAL
    assert len(reasons) == 1
    assert "85" in reasons[0]


def test_idle_clock_is_not_a_fault():
    state, _ = assess(gpu=GpuMetrics(util_pct=0, clock_mhz=300, clock_state=ClockState.IDLE))
    assert state is HealthState.GOOD


class TestDerivedBands:
    """Bands come from the silicon rather than from constants, because
    hardcoded numbers got this wrong from both directions: an 80C critical
    fired during ordinary work, while a 94C alert sat above the 90C at which a
    GB10 powers itself off and so could never have fired."""

    def test_gpu_bands_sit_on_the_slowdown_point(self):
        """Critical lands ON slowdown — the temperature where the hardware
        starts throttling, which is where performance actually degrades."""
        bands = TempThresholds.for_gpu(86.0)
        assert bands.critical_c == 86.0
        assert bands.warning_c == 82.0
        assert bands.source == "nvml-slowdown"

    def test_cpu_bands_sit_below_the_kernel_trip(self):
        """104C is where the kernel powers the machine off, so both bands stay
        clear of it — a cooling failure ramps fast and 2C is no notice."""
        bands = TempThresholds.for_cpu(104.0)
        assert bands.critical_c == 98.0
        assert bands.warning_c == 92.0
        assert bands.source == "acpi-critical-trip"

    def test_unreadable_limits_fall_back_and_say_so(self):
        """Not every part reports a limit. The fallback must be visibly a
        fallback, so a guess is never mistaken for a measurement."""
        for bands in (TempThresholds.for_gpu(None), TempThresholds.for_cpu(None)):
            assert bands.source == "fallback"
            assert bands.warning_c < bands.critical_c

    def test_nonsense_limits_are_rejected(self):
        """NVML reports 0 for unsupported thresholds; deriving from it would
        put critical at or below zero and flag every node critical forever."""
        assert TempThresholds.for_gpu(0).source == "fallback"
        assert TempThresholds.for_cpu(0).source == "fallback"

    def test_gb10_fallback_does_not_alarm_at_normal_load(self):
        """The GX10 runs 84C under routine ComfyUI generation without
        throttling. Whatever the fallback is, it must not call that critical —
        that regression is exactly what this replaced."""
        assert TempThresholds().critical_c > 84.0
