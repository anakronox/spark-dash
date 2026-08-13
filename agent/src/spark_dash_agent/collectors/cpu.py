"""Host CPU metrics.

Intentionally thin — `node_exporter` is the authority for host metrics and
Prometheus scrapes it directly. This exists only so the live-view snapshot is
self-contained, and so CPU temperature can feed node health without a PromQL
round trip.
"""

from __future__ import annotations

import os

import psutil
from spark_dash_common.models import CpuMetrics

from spark_dash_agent.collectors.base import Collector

# Preference order for which sensor represents "the CPU". GB10's are exposed
# via the ARM thermal zones; the x86-style names are here so the agent behaves
# sensibly on a dev box too.
_TEMP_SENSOR_PREFERENCE = ("coretemp", "k10temp", "cpu_thermal", "thermal_zone", "acpitz")


def _cpu_temp() -> float | None:
    """Best available CPU temperature, or None where unsupported (e.g. macOS)."""
    getter = getattr(psutil, "sensors_temperatures", None)
    if getter is None:
        return None
    try:
        sensors = getter()
    except Exception:  # noqa: BLE001 — unsupported platform
        return None
    if not sensors:
        return None

    for key in _TEMP_SENSOR_PREFERENCE:
        for label, entries in sensors.items():
            if label.startswith(key) and entries:
                return float(entries[0].current)

    # Nothing recognized: fall back to the hottest reading rather than nothing,
    # since an unknown-but-hot sensor is still worth surfacing.
    readings = [e.current for entries in sensors.values() for e in entries if e.current]
    return float(max(readings)) if readings else None


class CpuCollector(Collector[CpuMetrics]):
    name = "cpu"

    def collect(self) -> CpuMetrics:
        # interval=None returns usage since the previous call, which is what we
        # want on a repeating tick. A blocking interval would stall the loop.
        util = psutil.cpu_percent(interval=None)
        try:
            load1 = os.getloadavg()[0]
        except (OSError, AttributeError):
            load1 = None

        return CpuMetrics(
            util_pct=min(100.0, max(0.0, util)),
            temp_c=_cpu_temp(),
            load_avg_1m=load1,
            active_cores=psutil.cpu_count(logical=True),
        )
