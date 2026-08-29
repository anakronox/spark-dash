"""Network interfaces and RDMA ports.

Fixtures use the GX10's real interface names (enP2p1s0f0np0 and friends) and
the RoCE shape its ConnectX-7 presents: registered under /sys/class/infiniband
with link_layer Ethernet rather than InfiniBand.
"""

from spark_dash_agent.collectors.network import (
    NetworkCollector,
    RdmaCollector,
    _strip_enum,
)

# The GX10's real set: two ConnectX-7 ports, the onboard NIC, and the pile of
# bridges and veths a Docker host accumulates. Counters live in statistics/,
# which is where both collectors read them from.
GX10_INTERFACES = {
    "enP2p1s0f0np0": {
        "speed": 100000,
        "rx_bytes": 9876543210,
        "tx_bytes": 1234567890,
        "rx_dropped": 2,
        "tx_errors": 1,
    },
    "enP7s7": {"speed": 10000, "driver": "igc"},
    # Wireless: physical, but reading `speed` raises EINVAL rather than
    # returning a number. Real — this is the GX10's wlP9s9.
    "wlP9s9": {"speed_unreadable": True, "wireless": True, "driver": "mt7921e"},
    "docker0": {"physical": False, "speed": 10000},
    "br-05dc61118f7c": {"physical": False, "speed": 10000},
    "vethbeef01": {"physical": False, "speed": 10000},
}


def build_sysfs(tmp_path, interfaces):
    """Minimal /sys/class/net tree. `device` presence is what marks a NIC
    physical, which is how virtual interfaces are filtered out — note that
    docker0 and the veths report a speed just like a real NIC does, so speed
    is no help here.
    """
    for name, spec in interfaces.items():
        d = tmp_path / "class" / "net" / name
        (d / "statistics").mkdir(parents=True)
        (d / "operstate").write_text(spec.get("operstate", "up") + "\n")
        if "speed" in spec:
            (d / "speed").write_text(f"{spec['speed']}\n")
        elif spec.get("speed_unreadable"):
            # A directory stands in for the kernel's EINVAL: both surface as
            # OSError on read, which is the only thing the collector can see.
            (d / "speed").mkdir()
        if spec.get("physical", True):
            # The device symlink points INTO a bus tree, because that path is
            # how the collector tells USB from PCI. A bare directory would
            # report neither.
            bus = spec.get("bus", "pci")
            dev = tmp_path / "devices" / (
                "usb1/1-2/1-2:1.0" if bus == "usb" else "pci0000:00/0000:00:1f.6"
            ) / name
            dev.mkdir(parents=True)
            (d / "device").symlink_to(dev)
            if "driver" in spec:
                drv = tmp_path / "bus" / bus / "drivers" / spec["driver"]
                drv.mkdir(parents=True, exist_ok=True)
                (dev / "driver").symlink_to(drv)
        if spec.get("wireless"):
            (d / "wireless").mkdir()
        for counter in (
            "rx_bytes",
            "tx_bytes",
            "rx_errors",
            "tx_errors",
            "rx_dropped",
            "tx_dropped",
        ):
            (d / "statistics" / counter).write_text(f"{spec.get(counter, 0)}\n")
    return tmp_path


class TestNetworkCollector:
    def _collect(self, tmp_path, interfaces=None):
        sysfs = build_sysfs(tmp_path / "sys", interfaces or GX10_INTERFACES)
        return NetworkCollector(sysfs).collect()

    def test_reports_only_physical_interfaces(self, tmp_path):
        """A node running Docker has dozens of veth and bridge interfaces whose
        counters mean nothing — they'd bury the NICs that matter."""
        names = [i.name for i in self._collect(tmp_path)]
        assert names == ["enP2p1s0f0np0", "enP7s7", "wlP9s9"]

    def test_reads_link_speed(self, tmp_path):
        iface = self._collect(tmp_path)[0]
        # 100GbE ConnectX-7 reports 100000 Mb/s.
        assert iface.speed_mbps == 100000

    def test_first_sample_has_no_rate(self, tmp_path):
        """Nothing to compare against yet — must not invent throughput."""
        iface = self._collect(tmp_path)[0]
        assert iface.rx_bytes_per_sec == 0.0

    def test_totals_and_errors_are_reported(self, tmp_path):
        iface = self._collect(tmp_path)[0]
        assert iface.rx_bytes_total == 9876543210
        assert iface.tx_bytes_total == 1234567890
        assert iface.tx_errors == 1
        assert iface.rx_dropped == 2
        # Any error at all means not healthy — the count moving is the signal.
        assert iface.healthy is False

    def test_down_interface_reported_not_hidden(self, tmp_path):
        ifaces = self._collect(tmp_path, interfaces={"enP2p1s0f0np0": {"operstate": "down"}})
        assert ifaces[0].up is False

    def test_missing_speed_is_none_not_zero(self, tmp_path):
        """Absent is different from 'negotiated at 0'."""
        ifaces = self._collect(tmp_path, interfaces={"enP2p1s0f0np0": {}})
        assert ifaces[0].speed_mbps is None

    def test_unreadable_speed_is_none_not_a_failure(self, tmp_path):
        """The kernel returns EINVAL for `speed` on a wireless interface — the
        GX10's wlP9s9 does exactly this. It must report no speed rather than
        take down the whole collector, which is what enumerating every
        interface in sysfs newly exposes us to."""
        iface = next(i for i in self._collect(tmp_path) if i.name == "wlP9s9")
        assert iface.speed_mbps is None
        assert iface.up is True

    def test_down_port_speed_is_none_not_negative(self, tmp_path):
        """A down ConnectX-7 port reports -1, which must not reach the UI as a
        negotiated speed."""
        ifaces = self._collect(
            tmp_path, interfaces={"enP2p1s0f1np1": {"operstate": "down", "speed": -1}}
        )
        assert ifaces[0].speed_mbps is None

    def test_missing_counters_do_not_raise(self, tmp_path):
        sysfs = tmp_path / "sys"
        d = sysfs / "class" / "net" / "enP2p1s0f0np0"
        (d / "device").mkdir(parents=True)
        ifaces = NetworkCollector(sysfs).collect()
        assert len(ifaces) == 1
        assert ifaces[0].rx_bytes_total == 0

    def test_no_sysfs_yields_nothing(self, tmp_path):
        assert NetworkCollector(tmp_path / "nope").collect() == []

    def test_container_netns_does_not_hide_host_nics(self, tmp_path):
        """The regression this replaced. The agent enumerated /proc/net/dev,
        but /proc/net is a symlink to self/net and resolves through the READING
        process's network namespace — so even with the host's /proc mounted, a
        container saw only `lo` and its own `eth0`. Every host NIC vanished and
        the collector returned [] while reporting no error, which read as "this
        node has no network interfaces" rather than as a bug.

        A bind-mounted /sys carries the host's sysfs instance, tagged to the
        host's namespace, so the NICs are there no matter who reads it.
        """
        sysfs = build_sysfs(
            tmp_path / "sys",
            # The container's own eth0 is a veth: it is NOT in the host's
            # sysfs at all, so enumerating from there can't pick it up.
            GX10_INTERFACES,
        )
        names = [i.name for i in NetworkCollector(sysfs).collect()]
        assert names == ["enP2p1s0f0np0", "enP7s7", "wlP9s9"]
        assert "eth0" not in names


class TestRdmaCollector:
    """Fixtures mirror the GX10's real tree: devices named roceP2p1s0f0 rather
    than mlx5_0, link_layer Ethernet, and — critically — the InfiniBand-style
    byte counters sitting at zero on an ACTIVE link, because mlx5 doesn't
    populate them for RoCE."""

    def _build(
        self,
        tmp_path,
        *,
        device="roceP2p1s0f0",
        netdev="enP2p1s0f0np0",
        link_layer="Ethernet",
        state="4: ACTIVE",
        rate="100 Gb/sec (4X EDR)",
        netdev_rx=5_000_000,
        netdev_tx=3_000_000,
    ):
        dev = tmp_path / "class" / "infiniband" / device
        port = dev / "ports" / "1"
        (port / "counters").mkdir(parents=True)
        (port / "state").write_text(state + "\n")
        (port / "phys_state").write_text("5: LinkUp\n")
        (port / "link_layer").write_text(link_layer + "\n")
        (port / "rate").write_text(rate + "\n")
        # Zero, exactly as the GX10 reports on a live RoCE link.
        (port / "counters" / "port_rcv_data").write_text("0\n")
        (port / "counters" / "port_xmit_data").write_text("0\n")
        (port / "counters" / "port_rcv_errors").write_text("3\n")

        if netdev:
            # The RDMA device and its Ethernet interface share a PCI function.
            (dev / "device" / "net" / netdev).mkdir(parents=True)
            stats = tmp_path / "class" / "net" / netdev / "statistics"
            stats.mkdir(parents=True)
            (stats / "rx_bytes").write_text(f"{netdev_rx}\n")
            (stats / "tx_bytes").write_text(f"{netdev_tx}\n")
        return tmp_path

    def test_roce_is_detected(self, tmp_path):
        """The GX10's ConnectX-7 runs RoCEv2, so it registers here with
        link_layer Ethernet rather than InfiniBand."""
        ports = RdmaCollector(self._build(tmp_path)).collect()
        assert len(ports) == 1
        assert ports[0].link_layer == "Ethernet"
        assert ports[0].device == "roceP2p1s0f0"

    def test_traffic_comes_from_the_paired_netdev(self, tmp_path):
        """The regression this replaced: reading counters/port_rcv_data gave 0
        on a live RoCE link, so throughput would have shown 0 b/s forever —
        indistinguishable from an idle fabric rather than looking like a bug."""
        ports = RdmaCollector(self._build(tmp_path)).collect()
        assert ports[0].interface == "enP2p1s0f0np0"
        assert ports[0].rx_bytes_total == 5_000_000
        assert ports[0].tx_bytes_total == 3_000_000

    def test_no_paired_netdev_is_not_fatal(self, tmp_path):
        """Native InfiniBand has no Ethernet interface behind it."""
        ports = RdmaCollector(self._build(tmp_path, netdev=None)).collect()
        assert ports[0].interface == ""
        assert ports[0].rx_bytes_total == 0

    def test_state_ordinal_is_stripped(self, tmp_path):
        ports = RdmaCollector(self._build(tmp_path)).collect()
        assert ports[0].state == "ACTIVE"
        assert ports[0].active is True

    def test_inactive_port_is_reported(self, tmp_path):
        ports = RdmaCollector(self._build(tmp_path, state="1: DOWN")).collect()
        assert ports[0].state == "DOWN"
        assert ports[0].active is False

    def test_rate_is_surfaced_while_active(self, tmp_path):
        """A ConnectX-7 negotiating far below its rated speed is a known and
        otherwise invisible failure, so the raw string is kept."""
        ports = RdmaCollector(self._build(tmp_path, rate="10 Gb/sec (1X SDR)")).collect()
        assert ports[0].rate == "10 Gb/sec (1X SDR)"

    def test_rate_is_suppressed_while_down(self, tmp_path):
        """The driver reports a placeholder on a down port — the GX10 shows
        '40 Gb/sec (4X QDR)' on hardware rated far higher. Displaying it would
        look like a negotiation fault rather than an unused port."""
        ports = RdmaCollector(
            self._build(tmp_path, state="1: DOWN", rate="40 Gb/sec (4X QDR)")
        ).collect()
        assert ports[0].active is False
        assert ports[0].rate == ""

    def test_errors_are_summed(self, tmp_path):
        assert RdmaCollector(self._build(tmp_path)).collect()[0].errors == 3

    def test_no_rdma_hardware_yields_nothing(self, tmp_path):
        """The normal case on a standalone node."""
        assert RdmaCollector(tmp_path / "sys").collect() == []

    def test_missing_counters_do_not_raise(self, tmp_path):
        port = tmp_path / "class" / "infiniband" / "roceP2p1s0f0" / "ports" / "1"
        port.mkdir(parents=True)
        ports = RdmaCollector(tmp_path).collect()
        assert len(ports) == 1
        assert ports[0].rx_bytes_total == 0


def test_strip_enum():
    assert _strip_enum("4: ACTIVE") == "ACTIVE"
    assert _strip_enum("ACTIVE") == "ACTIVE"
    assert _strip_enum("") == ""


class TestInterfaceKind:
    """Three facts, not a verdict: the dashboard's RoCE / Management / WiFi /
    Other split needs the driver, the bus and the wireless flag, and none of
    the three can be told from a name."""

    def _one(self, tmp_path, name, spec):
        build_sysfs(tmp_path, {name: spec})
        (ifaces,) = NetworkCollector(tmp_path).collect()
        return ifaces

    def test_wireless_is_read_from_sysfs_not_the_name(self, tmp_path):
        i = self._one(tmp_path, "renamed0", {"speed_unreadable": True, "wireless": True})
        assert i.wireless is True
        assert self._one(tmp_path / "b", "wlP9s9", {"speed": 10000}).wireless is False

    def test_driver_is_the_symlink_basename(self, tmp_path):
        got = self._one(tmp_path, "enp1s0f1np1", {"speed": 100000, "driver": "mlx5_core"}).driver
        assert got == "mlx5_core"

    def test_no_driver_symlink_is_none_not_a_failure(self, tmp_path):
        assert self._one(tmp_path, "enP7s7", {"speed": 10000}).driver is None

    def test_bus_comes_from_the_device_path(self, tmp_path):
        got = self._one(tmp_path, "enx00e04c680001",
            {"speed": 1000, "bus": "usb", "driver": "r8152"}).bus
        assert got == "usb"
        assert self._one(tmp_path / "b", "enP7s7", {"speed": 10000}).bus == "pci"

    def test_older_snapshots_default_to_wired_unknown(self):
        """A node still on an older agent sends none of these; the model must
        fill them with values the dashboard treats as 'not known'."""
        from spark_dash_common.models import NetworkInterface

        i = NetworkInterface(name="enP7s7")
        assert (i.wireless, i.driver, i.bus) == (False, None, None)
