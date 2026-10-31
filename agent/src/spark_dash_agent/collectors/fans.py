"""Fan speeds from `/sys/class/hwmon` (GitHub issue #1).

ONLY WHERE THE FIRMWARE DECLARES A FAN. The kernel's `acpi_fan` driver binds
to ACPI fan devices (`PNP0C0B`, `ACPI000B`) and nothing else, so what a node
reports depends on its BIOS, not its kernel or DGX OS release. Measured
2026-10-31:

    node       board               BIOS                          fans
    sparketa   NVIDIA DGX Spark    5.36_0ACUM018                 none
    sparketa   NVIDIA DGX Spark    5.36_0ACUM028                 2, ~2150 rpm idle
    sparky     ASUS GX10           GX10DGX.0106.2026.0707.0731   none

Every chip with a `fan*_input` is read, not only `acpi_fan`, so a board that
exposes its fans through another driver is picked up without a change here.

The fans are also thermal cooling devices, and their `cur_state` read 0/100
while spinning at 2150 rpm, so the state is not read.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path

from spark_dash_common.models import FanSensor

from spark_dash_agent.collectors.base import Collector


def read_fans(sys_path: Path) -> list[FanSensor]:
    out: list[FanSensor] = []
    try:
        chips = list((sys_path / "class" / "hwmon").glob("hwmon*"))
    except OSError:
        return out

    for chip in chips:
        inputs = sorted(chip.glob("fan*_input"))
        if not inputs:
            continue
        try:
            name = (chip / "name").read_text().strip()
        except OSError:
            continue
        device = ""
        # Checked, not just resolved: resolve() of a missing link returns the
        # path itself, and the fan would be named `device`.
        if (chip / "device").exists():
            device = (chip / "device").resolve().name
        for fan_input in inputs:
            try:
                rpm = int(fan_input.read_text().strip())
            except (OSError, ValueError):
                # A fan whose read fails is absent this poll, never 0 rpm:
                # 0 is what a stopped fan reports.
                continue
            stem = fan_input.name.removesuffix("_input")
            label = None
            with suppress(OSError):
                label = (chip / f"{stem}_label").read_text().strip() or None
            parts = [name]
            if device and device != name:
                parts.append(device)
            parts.append(label or stem)
            out.append(FanSensor(sensor=" ".join(parts), rpm=rpm))
    # By name, so PNP0C0B:00 comes before :01 whatever hwmon number each got.
    return sorted(out, key=lambda f: f.sensor)


class FanCollector(Collector[list[FanSensor]]):
    name = "fans"

    def __init__(self, sys_path: Path) -> None:
        self._sys_path = sys_path

    def collect(self) -> list[FanSensor]:
        return read_fans(self._sys_path)
