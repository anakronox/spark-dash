"""The fan collector (GitHub issue #1), against sysfs shaped like sparketa's.

Read off sparketa 2026-10-31 after its BIOS went to 5.36_0ACUM028:

    hwmon0  acpi_fan  -> /sys/devices/platform/PNP0C0B:00  fan1_input 2147
    hwmon1  acpi_fan  -> /sys/devices/platform/PNP0C0B:01  fan1_input 2183

`fan1_input` is the only reading: no label, no min, no max, no target.
"""

from __future__ import annotations

from pathlib import Path

from spark_dash_agent.collectors.fans import FanCollector, read_fans
from spark_dash_common.models import FanSensor
from test_app import make_snapshot, render
from test_thermal import hwmon


def fan_chip(root: Path, index: int, *, rpm: str, device: str = "", name: str = "acpi_fan", stem: str = "fan1"):
    h = root / "class" / "hwmon" / f"hwmon{index}"
    h.mkdir(parents=True)
    (h / "name").write_text(f"{name}\n")
    (h / f"{stem}_input").write_text(f"{rpm}\n")
    if device:
        target = root / "devices" / "platform" / device
        target.mkdir(parents=True)
        (h / "device").symlink_to(target)
    return h


def test_reads_both_acpi_fans_in_device_order(tmp_path):
    # Numbered against device order on purpose: the hwmon index is whatever
    # probe order gave, and must not decide which fan is listed first.
    fan_chip(tmp_path, 7, rpm="2147", device="PNP0C0B:00")
    fan_chip(tmp_path, 1, rpm="2183", device="PNP0C0B:01")
    assert read_fans(tmp_path) == [
        FanSensor(sensor="acpi_fan PNP0C0B:00 fan1", rpm=2147),
        FanSensor(sensor="acpi_fan PNP0C0B:01 fan1", rpm=2183),
    ]


def test_a_board_without_fans_reports_none(tmp_path):
    """The ASUS GX10: temperature chips only."""
    hwmon(tmp_path, 0, name="nvme", device="nvme0", temps={"temp1": {"input": 49.85}})
    assert read_fans(tmp_path) == []
    assert FanCollector(tmp_path).collect() == []


def test_a_stopped_fan_is_zero_and_an_unreadable_one_is_absent(tmp_path):
    fan_chip(tmp_path, 0, rpm="0", device="PNP0C0B:00")
    fan_chip(tmp_path, 1, rpm="", device="PNP0C0B:01")
    assert read_fans(tmp_path) == [FanSensor(sensor="acpi_fan PNP0C0B:00 fan1", rpm=0)]


def test_fans_on_other_drivers_are_read_with_their_labels(tmp_path):
    h = fan_chip(tmp_path, 0, rpm="900", name="nct6775", stem="fan2")
    (h / "fan2_label").write_text("CPU fan\n")
    assert read_fans(tmp_path) == [FanSensor(sensor="nct6775 CPU fan", rpm=900)]


def test_no_hwmon_at_all(tmp_path):
    assert read_fans(tmp_path) == []


def test_exported_per_fan_and_absent_without_fans():
    text = render(make_snapshot(fans=[FanSensor(sensor="acpi_fan PNP0C0B:00 fan1", rpm=2147)]))
    assert 'sparkdash_fan_rpm{node="gx10-1",sensor="acpi_fan PNP0C0B:00 fan1"} 2147.0' in text
    assert "sparkdash_fan_rpm" not in render(make_snapshot())
