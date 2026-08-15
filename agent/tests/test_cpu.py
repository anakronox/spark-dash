

"""CPU thermal limits.

The CPU's bands are derived from its `critical` trip point rather than
hardcoded, for the same reason the GPU's come from NVML: the numbers that were
guessed turned out to be wrong in both directions.
"""

from spark_dash_agent.collectors.cpu import read_critical_trip_c


class TestCriticalTrip:
    """Read from sysfs because psutil returns critical=None for the GX10's
    acpitz zones even though the trip point is there."""

    def _zone(self, root, index, trips):
        z = root / "class" / "thermal" / f"thermal_zone{index}"
        z.mkdir(parents=True)
        for i, (kind, millidegrees) in enumerate(trips):
            (z / f"trip_point_{i}_type").write_text(kind + "\n")
            (z / f"trip_point_{i}_temp").write_text(f"{millidegrees}\n")
        return root

    def test_reads_the_gx10_shape(self, tmp_path):
        """Every acpitz zone on the GX10 reports critical at 104C."""
        for i in range(3):
            self._zone(tmp_path, i, [("critical", 104000)])
        assert read_critical_trip_c(tmp_path) == 104.0

    def test_lowest_trip_wins(self, tmp_path):
        """The machine is protected by whichever zone trips first."""
        self._zone(tmp_path, 0, [("critical", 104000)])
        self._zone(tmp_path, 1, [("critical", 95000)])
        assert read_critical_trip_c(tmp_path) == 95.0

    def test_non_critical_trips_are_ignored(self, tmp_path):
        """passive/hot trips throttle; only critical powers the machine off."""
        self._zone(tmp_path, 0, [("passive", 70000), ("hot", 90000), ("critical", 104000)])
        assert read_critical_trip_c(tmp_path) == 104.0

    def test_zero_and_sentinel_values_are_skipped(self, tmp_path):
        """0 means no trip configured, and some sensors report absurd
        sentinels — psutil showed 65261C on this box's nvme."""
        self._zone(tmp_path, 0, [("critical", 0)])
        self._zone(tmp_path, 1, [("critical", 65261850)])
        self._zone(tmp_path, 2, [("critical", 104000)])
        assert read_critical_trip_c(tmp_path) == 104.0

    def test_no_thermal_zones(self, tmp_path):
        assert read_critical_trip_c(tmp_path) is None

    def test_unreadable_root(self, tmp_path):
        assert read_critical_trip_c(tmp_path / "absent") is None
