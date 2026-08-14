"""Network interfaces and RDMA ports.

Fixtures use the GX10's real interface names (enP2p1s0f0np0 and friends) and
the RoCE shape its ConnectX-7 presents: registered under /sys/class/infiniband
with link_layer Ethernet rather than InfiniBand.
"""

from spark_dash_agent.collectors.network import (
    NetworkCollector,
    RdmaCollector,
    _read_proc_net_dev,
    _strip_enum,
)

# Two header lines then one row per interface, verbatim as the kernel writes
# it — deliberately not reflowed, since the point is to parse the real format.
# ruff: noqa: E501
PROC_NET_DEV = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 1000000    5000    0    0    0     0          0         0  1000000    5000    0    0    0     0       0          0
enP2p1s0f0np0: 9876543210 1234567    0    2    0     0          0         0  1234567890  654321    1    0    0     0       0          0
docker0:  500000    1200    0    0    0     0          0         0   400000    1000    0    0    0     0       0          0
vethbeef01:   12345      50    0    0    0     0          0         0     6789      30    0    0    0     0       0          0
"""


def build_sysfs(tmp_path, interfaces):
    """Minimal /sys/class/net tree. `device` presence is what marks a NIC
    physical, which is how virtual interfaces are filtered out."""
    for name, spec in interfaces.items():
        d = tmp_path / "class" / "net" / name
        d.mkdir(parents=True)
        (d / "operstate").write_text(spec.get("operstate", "up") + "\n")
        if "speed" in spec:
            (d / "speed").write_text(f"{spec['speed']}\n")
        if spec.get("physical", True):
            (d / "device").mkdir()
    return tmp_path


class TestProcNetDev:
    def test_parses_counters(self):
        stats = _read_proc_net_dev_from(PROC_NET_DEV)
        eth = stats["enP2p1s0f0np0"]
        assert eth["rx_bytes"] == 9876543210
        assert eth["tx_bytes"] == 1234567890
        assert eth["rx_drop"] == 2
        assert eth["tx_errs"] == 1

    def test_skips_headers(self):
        assert "Inter-|   Receive" not in _read_proc_net_dev_from(PROC_NET_DEV)

    def test_malformed_rows_are_skipped(self):
        stats = _read_proc_net_dev_from(PROC_NET_DEV + "broken: not numbers here\n")
        assert "broken" not in stats

    def test_empty_input(self):
        assert _read_proc_net_dev_from("") == {}


def _read_proc_net_dev_from(text):
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile("w", suffix=".dev", delete=False) as f:
        f.write(text)
        path = Path(f.name)
    return _read_proc_net_dev(path)


class TestNetworkCollector:
    def _collect(self, tmp_path, text=PROC_NET_DEV, interfaces=None):
        proc = tmp_path / "proc"
        (proc / "net").mkdir(parents=True)
        (proc / "net" / "dev").write_text(text)
        sysfs = build_sysfs(
            tmp_path / "sys",
            interfaces
            or {
                "enP2p1s0f0np0": {"speed": 200000},
                "docker0": {"physical": False},
                "vethbeef01": {"physical": False},
            },
        )
        return NetworkCollector(proc, sysfs).collect()

    def test_reports_only_physical_interfaces(self, tmp_path):
        """A node running Docker has dozens of veth interfaces whose counters
        mean nothing — they'd bury the NICs that matter."""
        names = [i.name for i in self._collect(tmp_path)]
        assert names == ["enP2p1s0f0np0"]

    def test_reads_link_speed(self, tmp_path):
        iface = self._collect(tmp_path)[0]
        # 200GbE ConnectX-7 reports 200000 Mb/s.
        assert iface.speed_mbps == 200000

    def test_first_sample_has_no_rate(self, tmp_path):
        """Nothing to compare against yet — must not invent throughput."""
        iface = self._collect(tmp_path)[0]
        assert iface.rx_bytes_per_sec == 0.0

    def test_totals_and_errors_are_reported(self, tmp_path):
        iface = self._collect(tmp_path)[0]
        assert iface.rx_bytes_total == 9876543210
        assert iface.tx_errors == 1
        assert iface.rx_dropped == 2
        # Any error at all means not healthy — the count moving is the signal.
        assert iface.healthy is False

    def test_down_interface_reported_not_hidden(self, tmp_path):
        ifaces = self._collect(
            tmp_path, interfaces={"enP2p1s0f0np0": {"operstate": "down"}}
        )
        assert ifaces[0].up is False

    def test_missing_speed_is_none_not_zero(self, tmp_path):
        """Absent is different from 'negotiated at 0'."""
        ifaces = self._collect(tmp_path, interfaces={"enP2p1s0f0np0": {}})
        assert ifaces[0].speed_mbps is None

    def test_no_procfs_yields_nothing(self, tmp_path):
        assert NetworkCollector(tmp_path / "nope", tmp_path / "sys").collect() == []


class TestRdmaCollector:
    def _build(
        self,
        tmp_path,
        *,
        link_layer="Ethernet",
        state="4: ACTIVE",
        rate="200 Gb/sec (2X NDR)",
    ):
        port = tmp_path / "class" / "infiniband" / "mlx5_0" / "ports" / "1"
        (port / "counters").mkdir(parents=True)
        (port / "state").write_text(state + "\n")
        (port / "phys_state").write_text("5: LinkUp\n")
        (port / "link_layer").write_text(link_layer + "\n")
        (port / "rate").write_text(rate + "\n")
        # Counters are in 4-byte words.
        (port / "counters" / "port_rcv_data").write_text("1000000\n")
        (port / "counters" / "port_xmit_data").write_text("2000000\n")
        (port / "counters" / "port_rcv_errors").write_text("3\n")
        return tmp_path

    def test_roce_is_detected(self, tmp_path):
        """The GX10's ConnectX-7 runs RoCEv2, so it registers here with
        link_layer Ethernet rather than InfiniBand."""
        ports = RdmaCollector(self._build(tmp_path)).collect()
        assert len(ports) == 1
        assert ports[0].link_layer == "Ethernet"
        assert ports[0].device == "mlx5_0"

    def test_counters_are_converted_from_words_to_bytes(self, tmp_path):
        """port_rcv_data is in 4-byte words. Missing that under-reports
        throughput by exactly 4x, which looks plausible rather than broken."""
        ports = RdmaCollector(self._build(tmp_path)).collect()
        assert ports[0].rx_bytes_total == 4_000_000
        assert ports[0].tx_bytes_total == 8_000_000

    def test_state_ordinal_is_stripped(self, tmp_path):
        ports = RdmaCollector(self._build(tmp_path)).collect()
        assert ports[0].state == "ACTIVE"
        assert ports[0].active is True

    def test_inactive_port_is_reported(self, tmp_path):
        ports = RdmaCollector(self._build(tmp_path, state="1: DOWN")).collect()
        assert ports[0].state == "DOWN"
        assert ports[0].active is False

    def test_rate_is_surfaced(self, tmp_path):
        """A ConnectX-7 negotiating far below its rated speed is a known and
        otherwise invisible failure, so the raw string is kept."""
        ports = RdmaCollector(self._build(tmp_path, rate="10 Gb/sec (1X SDR)")).collect()
        assert ports[0].rate == "10 Gb/sec (1X SDR)"

    def test_errors_are_summed(self, tmp_path):
        assert RdmaCollector(self._build(tmp_path)).collect()[0].errors == 3

    def test_no_rdma_hardware_yields_nothing(self, tmp_path):
        """The normal case on a standalone node."""
        assert RdmaCollector(tmp_path / "sys").collect() == []

    def test_missing_counters_do_not_raise(self, tmp_path):
        port = tmp_path / "class" / "infiniband" / "mlx5_0" / "ports" / "1"
        port.mkdir(parents=True)
        ports = RdmaCollector(tmp_path).collect()
        assert len(ports) == 1
        assert ports[0].rx_bytes_total == 0


def test_strip_enum():
    assert _strip_enum("4: ACTIVE") == "ACTIVE"
    assert _strip_enum("ACTIVE") == "ACTIVE"
    assert _strip_enum("") == ""
