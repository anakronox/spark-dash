"""System memory, computed correctly for GB10's coherent unified pool.

The whole reason this collector exists: on GB10 there is no private VRAM. CPU
and GPU share one coherent LPDDR5x pool, and `nvmlDeviceGetMemoryInfo` reports
`total ≈ MemTotal` more or less regardless of what's actually allocated — so
the number every standard GPU exporter shows is meaningless here.

`MemAvailable` is the honest source, and `total - available` is what actually
tracks reality under heavy inference load. This mirrors sparkview's approach.
"""

from __future__ import annotations

import psutil
from spark_dash_common.models import MemoryMetrics

from spark_dash_agent.collectors.base import Collector

# NVML total within this fraction of system total means they're describing the
# same physical memory — i.e. a unified pool rather than discrete VRAM.
_UMA_DETECT_TOLERANCE = 0.10


def detect_unified_memory(nvml_total_bytes: int | None, system_total_bytes: int) -> bool:
    """Decide whether GPU and system memory are one coherent pool.

    Runtime detection rather than a hardcoded "is this a GB10" check, so the
    agent behaves correctly on a discrete-GPU box too (e.g. a dev machine)
    without needing to know the hardware in advance.
    """
    if not nvml_total_bytes or system_total_bytes <= 0:
        return False
    delta = abs(nvml_total_bytes - system_total_bytes) / system_total_bytes
    return delta < _UMA_DETECT_TOLERANCE


class MemoryCollector(Collector[MemoryMetrics]):
    name = "memory"

    def __init__(self, unified: bool = False) -> None:
        # UMA-ness is a property of the hardware, so it's detected once at
        # startup and passed in rather than re-derived every tick.
        self._unified = unified

    def collect(self) -> MemoryMetrics:
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()

        # Deliberately NOT psutil's `vm.used`: its cache/buffer accounting
        # differs across platforms, and MemAvailable is the value the kernel
        # itself considers obtainable without swapping.
        used = max(0, vm.total - vm.available)

        return MemoryMetrics(
            total_bytes=vm.total,
            available_bytes=vm.available,
            used_bytes=used,
            swap_used_bytes=swap.used,
            unified=self._unified,
        )
