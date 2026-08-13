"""Memory pressure from /proc/pressure/memory.

PSI is the signal that catches contention *before* swap thrashing or a freeze —
percent-used alone will read a comfortable 70% right up until the box stalls.
On a node deliberately packed with model weights it's the more honest memory
health indicator of the two.
"""

from __future__ import annotations

from pathlib import Path

from spark_dash_common.models import PsiMetrics, PsiState
from spark_dash_common.thresholds import PSI_BANDS, PsiBands

from spark_dash_agent.collectors.base import Collector

PSI_MEMORY_PATH = Path("/proc/pressure/memory")


def parse_psi(content: str) -> dict[str, float]:
    """Parse the two-line PSI format into a flat dict.

        some avg10=0.00 avg60=0.00 avg300=0.00 total=0
        full avg10=0.00 avg60=0.00 avg300=0.00 total=0

    Yields keys like `some_avg10`, `full_avg60`. Unknown lines are ignored so a
    kernel that adds a third row doesn't break us.
    """
    values: dict[str, float] = {}
    for line in content.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] not in ("some", "full"):
            continue
        prefix = parts[0]
        for field in parts[1:]:
            key, _, raw = field.partition("=")
            if not raw:
                continue
            try:
                values[f"{prefix}_{key}"] = float(raw)
            except ValueError:
                continue
    return values


def classify(some_avg10: float, full_avg10: float, bands: PsiBands = PSI_BANDS) -> PsiState:
    """Map raw stall percentages onto a pressure band.

    `full` (every task stalled) is weighted harder than `some` (at least one),
    since it means nothing is making progress. The worse of the two wins.
    """
    if full_avg10 >= bands.full_critical or some_avg10 >= bands.some_critical:
        return PsiState.CRITICAL
    if full_avg10 >= bands.full_high or some_avg10 >= bands.some_high:
        return PsiState.HIGH
    if full_avg10 >= bands.full_mod or some_avg10 >= bands.some_mod:
        return PsiState.MOD
    return PsiState.LOW


class PsiCollector(Collector[PsiMetrics]):
    name = "psi"

    def __init__(self, path: Path = PSI_MEMORY_PATH, bands: PsiBands = PSI_BANDS) -> None:
        self._path = path
        self._bands = bands

    def collect(self) -> PsiMetrics | None:
        # Absent on non-Linux and on kernels built without CONFIG_PSI. That's a
        # normal "nothing to report", not an error worth surfacing.
        if not self._path.exists():
            return None

        values = parse_psi(self._path.read_text())
        some_avg10 = values.get("some_avg10", 0.0)
        full_avg10 = values.get("full_avg10", 0.0)
        return PsiMetrics(
            some_avg10=some_avg10,
            some_avg60=values.get("some_avg60", 0.0),
            full_avg10=full_avg10,
            full_avg60=values.get("full_avg60", 0.0),
            state=classify(some_avg10, full_avg10, self._bands),
        )
