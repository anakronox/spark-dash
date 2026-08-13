"""Unified-memory handling is the single most important correctness property
of this agent — every standard GPU exporter gets it wrong on GB10.
"""

from spark_dash_agent.collectors.memory import MemoryCollector, detect_unified_memory

GB10_POOL = 128 * 1024**3


def test_detects_uma_when_nvml_total_matches_system_total():
    """GB10: NVML reports the whole shared pool, so the totals coincide."""
    assert detect_unified_memory(GB10_POOL, GB10_POOL) is True


def test_detects_uma_within_tolerance():
    """Totals differ slightly (firmware reservations) but describe one pool."""
    assert detect_unified_memory(int(GB10_POOL * 0.96), GB10_POOL) is True


def test_discrete_gpu_is_not_unified():
    """24GB of VRAM beside 128GB of RAM is clearly not one pool."""
    assert detect_unified_memory(24 * 1024**3, GB10_POOL) is False


def test_no_nvml_total_is_not_unified():
    assert detect_unified_memory(None, GB10_POOL) is False
    assert detect_unified_memory(0, GB10_POOL) is False


def test_zero_system_total_does_not_divide_by_zero():
    assert detect_unified_memory(GB10_POOL, 0) is False


def test_used_is_total_minus_available(monkeypatch):
    """Not psutil's `used`: MemAvailable is what the kernel actually considers
    obtainable, and it's the number that tracks reality under inference load."""

    class FakeVM:
        total = 1000
        available = 300

    class FakeSwap:
        used = 42

    monkeypatch.setattr("psutil.virtual_memory", lambda: FakeVM())
    monkeypatch.setattr("psutil.swap_memory", lambda: FakeSwap())

    metrics = MemoryCollector(unified=True).collect()
    assert metrics.used_bytes == 700
    assert metrics.swap_used_bytes == 42
    assert metrics.unified is True
    assert metrics.used_pct == 70.0


def test_used_never_goes_negative(monkeypatch):
    """available > total is nonsense, but must not produce a negative gauge."""

    class FakeVM:
        total = 100
        available = 150

    class FakeSwap:
        used = 0

    monkeypatch.setattr("psutil.virtual_memory", lambda: FakeVM())
    monkeypatch.setattr("psutil.swap_memory", lambda: FakeSwap())

    assert MemoryCollector().collect().used_bytes == 0


def test_used_pct_handles_zero_total():
    from spark_dash_common.models import MemoryMetrics

    metrics = MemoryMetrics(total_bytes=0, available_bytes=0, used_bytes=0)
    assert metrics.used_pct == 0.0
