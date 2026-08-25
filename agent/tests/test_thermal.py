"""The thermal collector, against a sysfs tree built from measured hardware.

Every fixture here mirrors what a GB10 actually exposes, read off sparky and
sparketa on 2026-08-25:

    hwmon name      device            label       crit     domain
    acpitz          thermal_zone0     -           -        skipped
    nvme            nvme0             Composite   84.85    storage
    mlx5            0000:01:00.0      asic        105.0    network
    mt7925_phy0     phy0              -           -        wireless

plus seven `/sys/class/thermal/thermal_zone*` all of type `acpitz` with a single
`critical` trip at 104.8 C.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spark_dash_agent.collectors.thermal import (
    ThermalCollector,
    classify,
    read_hwmon,
    read_thermal_zones,
)


def zone(root: Path, index: int, *, temp_c: float, kind: str = "acpitz", trip_c: float | None = 104.8):
    z = root / "class" / "thermal" / f"thermal_zone{index}"
    z.mkdir(parents=True)
    (z / "type").write_text(f"{kind}\n")
    (z / "temp").write_text(f"{int(temp_c * 1000)}\n")
    if trip_c is not None:
        (z / "trip_point_0_type").write_text("critical\n")
        (z / "trip_point_0_temp").write_text(f"{int(trip_c * 1000)}\n")
    return z


def hwmon(root: Path, index: int, *, name: str, temps: dict[str, dict], device: str | None = None):
    """`temps` maps tempN -> {input, label?, crit?, max?} in degrees."""
    h = root / "class" / "hwmon" / f"hwmon{index}"
    h.mkdir(parents=True)
    (h / "name").write_text(f"{name}\n")
    if device:
        target = root / "devices" / device
        target.mkdir(parents=True, exist_ok=True)
        (h / "device").symlink_to(target)
    for stem, spec in temps.items():
        (h / f"{stem}_input").write_text(f"{int(spec['input'] * 1000)}\n")
        for key in ("label",):
            if key in spec:
                (h / f"{stem}_{key}").write_text(f"{spec[key]}\n")
        for key in ("crit", "max"):
            if key in spec:
                (h / f"{stem}_{key}").write_text(f"{int(spec[key] * 1000)}\n")
    return h


@pytest.fixture
def gb10(tmp_path: Path) -> Path:
    """A sysfs tree shaped like a fully-populated GB10."""
    for i, t in enumerate([74.5, 59.0, 58.2, 59.8, 67.8, 74.5, 61.8]):
        zone(tmp_path, i, temp_c=t)
    # hwmon0 IS thermal_zone0's hwmon child and republishes all seven zones.
    hwmon(
        tmp_path, 0, name="acpitz",
        temps={f"temp{i + 1}": {"input": t} for i, t in enumerate([74.5, 59.0, 58.2, 59.8, 67.8, 74.5, 61.8])},
    )
    hwmon(
        tmp_path, 1, name="nvme", device="nvme0",
        temps={
            "temp1": {"input": 49.85, "label": "Composite", "crit": 84.85, "max": 82.85},
            # 65261.85 is what this hardware really reports for temp2_max.
            "temp2": {"input": 51.85, "label": "Sensor 1", "max": 65261.85},
        },
    )
    hwmon(tmp_path, 2, name="mlx5", device="0000:01:00.0",
          temps={"temp1": {"input": 52.0, "label": "asic", "crit": 105.0}})
    hwmon(tmp_path, 3, name="mlx5", device="0000:01:00.1",
          temps={"temp1": {"input": 52.0, "label": "asic", "crit": 105.0}})
    hwmon(tmp_path, 4, name="mt7925_phy0", device="phy0", temps={"temp1": {"input": 42.0}})
    return tmp_path


# --------------------------------------------------------------- classify

@pytest.mark.parametrize(
    "chip,domain",
    [
        ("acpitz", "package"),
        ("coretemp", "package"),
        ("nvme", "storage"),
        ("mlx5", "network"),
        ("mt7925_phy0", "wireless"),
        ("ieee80211_phy0", "wireless"),
    ],
)
def test_measured_chips_classify(chip, domain):
    assert classify(chip) == domain


def test_an_unknown_chip_is_other_not_dropped():
    """A sensor nobody anticipated is exactly the one worth seeing. A filter
    that silently discards what it does not recognise is how a new thermal
    problem stays invisible on hardware the author never had."""
    assert classify("some_future_pmic") == "other"


# ------------------------------------------------------------ the zones

def test_every_zone_is_read_with_its_own_trip(gb10):
    zones = read_thermal_zones(gb10)
    assert len(zones) == 7
    assert [z.sensor for z in zones] == [f"zone{i}" for i in range(7)]
    assert all(z.domain == "package" for z in zones)
    assert all(z.limit_c == pytest.approx(104.8) for z in zones)


def test_zones_are_named_by_index_not_by_type(gb10):
    """All seven report type `acpitz`, so the type names none of them."""
    assert {z.sensor for z in read_thermal_zones(gb10)} == {f"zone{i}" for i in range(7)}


def test_a_zone_with_no_trip_reports_no_limit(tmp_path):
    zone(tmp_path, 0, temp_c=50.0, trip_c=None)
    (sensor,) = read_thermal_zones(tmp_path)
    assert sensor.limit_c is None
    assert sensor.headroom_c is None


# ------------------------------------------------------------- the hwmon

def test_the_acpitz_hwmon_chip_is_skipped(gb10):
    """THE DOUBLE-COUNT. hwmon0 IS thermal_zone0's hwmon child and republishes
    all seven zones as temp1..temp7. Walking both sources without skipping it
    gives every package sensor twice — which inflates nothing visibly, because
    the max is still the max, and silently doubles every row count."""
    chips = read_hwmon(gb10)
    assert not [s for s in chips if s.domain == "package"], (
        "the acpitz hwmon chip was read as well as the thermal zones"
    )


def test_the_whole_tree_has_no_duplicate_sensor_names(gb10):
    sensors = ThermalCollector(gb10).collect()
    names = [s.sensor for s in sensors]
    assert len(names) == len(set(names)), f"duplicate sensors: {names}"


def test_identical_chips_are_told_apart_by_device(gb10):
    """Two mlx5 chips share a name; only the PCI address distinguishes them."""
    nics = [s for s in ThermalCollector(gb10).collect() if s.domain == "network"]
    assert len(nics) == 2
    assert {s.sensor for s in nics} == {
        "mlx5 0000:01:00.0 asic",
        "mlx5 0000:01:00.1 asic",
    }


def test_a_label_is_preferred_over_the_bare_temp_index(gb10):
    storage = [s.sensor for s in ThermalCollector(gb10).collect() if s.domain == "storage"]
    assert any("Composite" in s for s in storage), storage
    assert not any(s.endswith("temp1") for s in storage), storage


def test_a_sentinel_limit_is_refused(gb10):
    """nvme really reports 65261.85 C for temp2_max on this hardware. A limit of
    65261 makes every headroom meaningless while looking like data."""
    sensor = next(s for s in ThermalCollector(gb10).collect() if "Sensor 1" in s.sensor)
    assert sensor.limit_c is None


def test_max_is_used_when_a_chip_states_no_crit(tmp_path):
    """Hardware not present on this cluster, and the reason the fallback exists.

    Every chip here that has a `max` also has a `crit`, so nothing observable
    exercises this path — which is exactly why it needs a test rather than a
    measurement. A chip stating only its rated maximum has still stated a limit,
    and dropping it would leave that sensor with no headroom at all.
    """
    hwmon(tmp_path, 0, name="somechip", temps={"temp1": {"input": 40.0, "max": 70.0}})
    (sensor,) = read_hwmon(tmp_path)
    assert sensor.limit_c == pytest.approx(70.0)


def test_crit_wins_over_max(gb10):
    """Both exist on the nvme Composite: 84.85 crit against 82.85 max. `crit` is
    the one the hardware calls fatal."""
    sensor = next(s for s in ThermalCollector(gb10).collect() if "Composite" in s.sensor)
    assert sensor.limit_c == pytest.approx(84.85)


# ---------------------------------------------------------------- shapes

def test_the_full_gb10_inventory(gb10):
    sensors = ThermalCollector(gb10).collect()
    counts: dict[str, int] = {}
    for s in sensors:
        counts[s.domain] = counts.get(s.domain, 0) + 1
    # 7 zones + 2 nvme + 2 NIC + 1 wifi. The GPU is added by the exporter.
    assert counts == {"package": 7, "storage": 2, "network": 2, "wireless": 1}


def test_a_node_with_no_nics_still_works(tmp_path):
    """sparky reports no mlx5 chips at all. A per-node card has to tolerate
    different sensor sets rather than assume the fullest one."""
    zone(tmp_path, 0, temp_c=69.2)
    hwmon(tmp_path, 1, name="nvme", device="nvme0",
          temps={"temp1": {"input": 49.85, "label": "Composite", "crit": 84.85}})
    sensors = ThermalCollector(tmp_path).collect()
    assert {s.domain for s in sensors} == {"package", "storage"}


def test_a_machine_with_no_sensors_reports_nothing_rather_than_failing(tmp_path):
    """Any macOS dev machine, and CI."""
    assert ThermalCollector(tmp_path).collect() == []


def test_an_unreadable_temp_is_skipped_not_zeroed(tmp_path):
    """The wifi phy reports an EMPTY temp1 when the radio is down — measured on
    all three nodes. Zero would read as a real, very cold sensor and would win
    every headroom comparison on the card."""
    z = zone(tmp_path, 0, temp_c=50.0)
    (z / "temp").write_text("\n")
    assert read_thermal_zones(tmp_path) == []
