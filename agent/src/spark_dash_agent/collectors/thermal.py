"""Every temperature sensor the machine exposes, classified by what it measures.

WHY THIS EXISTS. The agent reported two temperatures — a GPU reading from NVML
and one CPU reading psutil happened to pick first. A GB10 exposes eighteen to
twenty-three. Measured over 24h on this cluster, `acpitz` zone0 peaked at
**95.4 °C while the GPU read 72.0 °C at the same instant**: a sensor 23 degrees
hotter than the one the dashboard led with, and nothing looking at it.

CLASSIFIED BY DOMAIN, NOT BY LOCATION. The seven `acpitz` zones are unlabelled
and every one of them correlates 0.89-0.99 with GPU temperature, because a GB10
is a single package — there is no chassis-versus-die separation to find. What
does separate cleanly is the COMPONENT: package, storage, network, wireless.
Each has its own limit, and those limits differ by twenty degrees, so a domain
is the smallest unit against which a reading can be judged.

Measured on this hardware, and the whole basis of `classify()`:

    hwmon name      device            label       crit     domain
    acpitz          thermal_zone0     -           -        (skipped, see below)
    nvme            nvme0             Composite   84.85    storage
    mlx5            0000:01:00.0      asic        105.0    network
    mt7925_phy0     phy0              -           -        wireless

WHAT IS NOT HERE, because it does not exist on this hardware: fans, power,
voltage and current. lm-sensors sees three chips and all three are
temperature-only. Nothing in this file should grow an airflow or wattage story
without someone first checking that the sensors appeared.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from pathlib import Path

from spark_dash_common.models import TempSensor

from spark_dash_agent.collectors.base import Collector

log = logging.getLogger(__name__)

#: hwmon chip names that are ALREADY covered by /sys/class/thermal.
#:
#: THE DOUBLE-COUNT, and it is not hypothetical. `hwmon0` on these nodes IS
#: `thermal_zone0`'s hwmon child, and it publishes all seven zones as
#: temp1..temp7 — while `/sys/class/thermal/thermal_zone0..6` publishes the same
#: seven. Walking both without this gives every package sensor twice, which
#: inflates nothing visibly (the max is the max) and doubles the row count on
#: any table, silently.
#:
#: Skipping the HWMON side rather than the thermal side is deliberate: only the
#: zones carry trip points, which is where the 104.8 °C limit comes from.
HWMON_ALIASES_OF_THERMAL = frozenset({"acpitz"})

#: chip name prefix -> domain. Longest match wins, so `mt7925_phy0` resolves
#: without needing an entry per radio.
_DOMAINS: tuple[tuple[str, str], ...] = (
    ("nvme", "storage"),
    ("mlx5", "network"),
    ("mt7925", "wireless"),
    ("iwlwifi", "wireless"),
    ("ieee80211", "wireless"),
    ("coretemp", "package"),
    ("k10temp", "package"),
    ("cpu_thermal", "package"),
    ("acpitz", "package"),
)

#: A trip point above this is a sentinel rather than a temperature. The kernel
#: uses absurd values for "not configured" — nvme reports 65261.85 °C for
#: `temp2_max` on this very hardware — and a limit of 65261 makes every headroom
#: calculation meaningless while looking like data.
_MAX_PLAUSIBLE_C = 200.0


def classify(chip: str) -> str:
    """Which component a chip measures.

    UNKNOWN BECOMES `other`, NEVER DROPPED. A sensor nobody anticipated is
    exactly the one worth seeing — a filter that silently discards what it does
    not recognise is how a new thermal problem stays invisible on hardware the
    author never had.
    """
    name = chip.lower()
    for prefix, domain in _DOMAINS:
        if name.startswith(prefix):
            return domain
    return "other"


def _read_c(path: Path) -> float | None:
    """A sysfs millidegree file as °C, or None if it is absent or a sentinel."""
    try:
        value = int(path.read_text().strip()) / 1000.0
    except (OSError, ValueError):
        return None
    # 0 means "no trip configured" on a limit, and is a real reading of freezing
    # nowhere in a datacentre. Both are noise.
    if not 0 < value < _MAX_PLAUSIBLE_C:
        return None
    return value


def _zone_limit_c(zone: Path) -> float | None:
    """The zone's own `critical` trip — where the kernel powers the machine off.

    Read here rather than taken from `read_critical_trip_c` in the CPU collector
    because that one deliberately returns the LOWEST trip across all zones, for
    deriving a single band. This wants each zone's own, so a zone with a
    different trip is judged against it.
    """
    for trip_type in sorted(zone.glob("trip_point_*_type")):
        try:
            if trip_type.read_text().strip() != "critical":
                continue
        except OSError:
            continue
        limit = _read_c(trip_type.with_name(trip_type.name.replace("_type", "_temp")))
        if limit is not None:
            return limit
    return None


def read_thermal_zones(sys_path: Path) -> list[TempSensor]:
    """The `/sys/class/thermal` zones — the package sensors on a GB10."""
    out: list[TempSensor] = []
    try:
        zones = sorted(
            (sys_path / "class" / "thermal").glob("thermal_zone*"),
            key=lambda p: int(p.name.removeprefix("thermal_zone") or 0),
        )
    except (OSError, ValueError):
        return out

    for zone in zones:
        celsius = _read_c(zone / "temp")
        if celsius is None:
            continue
        try:
            kind = (zone / "type").read_text().strip()
        except OSError:
            kind = "thermal"
        index = zone.name.removeprefix("thermal_zone")
        out.append(
            TempSensor(
                domain=classify(kind),
                # `zone3`, not `acpitz` seven times over. Every zone here
                # reports the same type, so the type alone names none of them.
                sensor=f"zone{index}",
                celsius=celsius,
                limit_c=_zone_limit_c(zone),
            )
        )
    return out


def read_hwmon(sys_path: Path) -> list[TempSensor]:
    """Every `/sys/class/hwmon` chip that is not already a thermal zone."""
    out: list[TempSensor] = []
    try:
        chips = sorted((sys_path / "class" / "hwmon").glob("hwmon*"))
    except OSError:
        return out

    for chip in chips:
        try:
            name = (chip / "name").read_text().strip()
        except OSError:
            continue
        if name in HWMON_ALIASES_OF_THERMAL:
            continue

        domain = classify(name)
        for temp_input in sorted(chip.glob("temp*_input")):
            celsius = _read_c(temp_input)
            if celsius is None:
                continue
            stem = temp_input.name.removesuffix("_input")
            label = None
            with suppress(OSError):
                label = (chip / f"{stem}_label").read_text().strip() or None
            # The chip's own label where it has one -- `Composite` says more
            # than `temp1` -- and the device address where several identical
            # chips share a name, which is every mlx5 on this hardware.
            device = ""
            with suppress(OSError):
                device = (chip / "device").resolve().name
            parts = [name]
            if device and device != name:
                parts.append(device)
            parts.append(label or stem)
            out.append(
                TempSensor(
                    domain=domain,
                    sensor=" ".join(parts),
                    celsius=celsius,
                    limit_c=_read_c(chip / f"{stem}_crit")
                    or _read_c(chip / f"{stem}_max"),
                )
            )
    return out


class ThermalCollector(Collector[list[TempSensor]]):
    name = "thermal"

    def __init__(self, sys_path: Path) -> None:
        self._sys_path = sys_path

    def collect(self) -> list[TempSensor]:
        """Zones first, then hwmon — the order the card reads in.

        The GPU is NOT here. It comes from NVML in the GPU collector, which
        already reads its slowdown and shutdown thresholds; duplicating it
        through sysfs would give a second answer to a question that already has
        one. The exporter joins it in.
        """
        return read_thermal_zones(self._sys_path) + read_hwmon(self._sys_path)
