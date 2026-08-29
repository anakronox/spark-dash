"""Network interfaces and RDMA ports.

Two collectors that read sysfs directly rather than going through
`node_exporter`. That duplication is deliberate: node_exporter is scraped at
15s and feeds history, but the live view needs sub-2s throughput to be worth
looking at.

RDMA matters here specifically because the GX10s cluster over ConnectX-7 using
RoCEv2 — RDMA over Ethernet rather than native InfiniBand. The devices still
register under /sys/class/infiniband either way, so that's what this reads;
`link_layer` is what tells you which mode you're in.
"""

from __future__ import annotations

import logging
from pathlib import Path

from spark_dash_common.models import NetworkInterface, RdmaPort

from spark_dash_agent.collectors.base import Collector
from spark_dash_agent.collectors.llama_router import RateTracker

log = logging.getLogger(__name__)

# Interfaces that exist but tell an operator nothing. A node running Docker
# accumulates dozens of these, and they'd bury the NICs that matter.
_VIRTUAL_PREFIXES = ("lo", "veth", "docker", "br-", "virbr", "tap", "tun", "cni", "flannel")


class NetworkCollector(Collector[list[NetworkInterface]]):
    """Physical interfaces, with throughput derived between samples.

    ENUMERATED FROM SYSFS, NOT /proc/net/dev. Verified on the GX10: the agent
    container has its own network namespace, and /proc/net is a symlink to
    self/net, which resolves through the *reading* process's namespace. So even
    with the host's /proc bind-mounted, /host/proc/net/dev listed only `lo` and
    the container's own `eth0` — every host NIC was invisible and the collector
    returned an empty list while reporting no error at all.

    A bind-mounted /sys carries the host's sysfs instance, which is tagged to
    the host's network namespace, so /host/sys/class/net lists the real NICs
    regardless of where it's read from. That's why the RDMA collector — reading
    the same statistics/ files — worked throughout.
    """

    name = "network"

    def __init__(self, sys_path: Path) -> None:
        self._sys_path = sys_path
        self._net_root = sys_path / "class" / "net"
        self._rates = RateTracker()

    def collect(self) -> list[NetworkInterface]:
        try:
            names = sorted(p.name for p in self._net_root.iterdir())
        except OSError:
            return []

        interfaces: list[NetworkInterface] = []
        live_keys: set[str] = set()

        for name in names:
            if not self._is_physical(name):
                continue

            stats = self._net_root / name / "statistics"
            rx_bytes = _read_int(stats / "rx_bytes")
            tx_bytes = _read_int(stats / "tx_bytes")
            rx_key, tx_key = f"{name}:rx", f"{name}:tx"
            live_keys |= {rx_key, tx_key}

            interfaces.append(
                NetworkInterface(
                    name=name,
                    up=self._is_up(name),
                    wireless=self._is_wireless(name),
                    driver=self._driver(name),
                    bus=self._bus(name),
                    speed_mbps=self._speed(name),
                    rx_bytes_per_sec=self._rates.rate(rx_key, rx_bytes),
                    tx_bytes_per_sec=self._rates.rate(tx_key, tx_bytes),
                    rx_bytes_total=rx_bytes,
                    tx_bytes_total=tx_bytes,
                    rx_errors=_read_int(stats / "rx_errors"),
                    tx_errors=_read_int(stats / "tx_errors"),
                    rx_dropped=_read_int(stats / "rx_dropped"),
                    tx_dropped=_read_int(stats / "tx_dropped"),
                )
            )

        self._rates.forget(live_keys)
        return interfaces

    def _is_physical(self, name: str) -> bool:
        """Physical NICs have a `device` symlink in sysfs.

        Checked by symlink rather than by name pattern: an interface can be
        renamed to anything, and predictable-names schemes vary. The prefix
        list is only a fast reject for the common virtual cases.
        """
        if name.startswith(_VIRTUAL_PREFIXES):
            return False
        return (self._sys_path / "class" / "net" / name / "device").exists()

    # WHAT KIND OF NIC, as three facts rather than one verdict.
    #
    # The dashboard divides interfaces into RoCE, Management, WiFi and Other,
    # and the last three cannot be told apart by name: a ConnectX-7 port with
    # no RDMA pairing looks exactly like the onboard 10G (`enP7s7` vs
    # `enP2p1s0f0np0`), and a USB adapter can be renamed to anything. sysfs
    # knows -- the driver, the bus, and whether there is a `wireless/`
    # directory -- so those are reported as they are. The RULE that turns them
    # into a group lives in the frontend, where it can change without a
    # redeploy of every node.

    def _is_wireless(self, name: str) -> bool:
        d = self._sys_path / "class" / "net" / name
        return (d / "wireless").is_dir() or (d / "phy80211").exists()

    def _driver(self, name: str) -> str | None:
        """Basename of the device/driver symlink, e.g. `mlx5_core`."""
        link = self._sys_path / "class" / "net" / name / "device" / "driver"
        # is_symlink first: resolve() on a path that does not exist returns
        # the path itself, whose basename is the word "driver".
        if not link.is_symlink():
            return None
        try:
            return link.resolve().name or None
        except OSError:
            return None

    def _bus(self, name: str) -> str | None:
        """`usb` or `pci`, read off the device symlink's resolved path.

        A USB NIC's device sits under .../usbN/...; a PCI one under
        .../pci0000:00/.... Anything else is reported as None rather than
        guessed, and the dashboard treats None as "not known" rather than as
        either.
        """
        link = self._sys_path / "class" / "net" / name / "device"
        try:
            parts = link.resolve().parts
        except OSError:
            return None
        if any(p.startswith("usb") for p in parts):
            return "usb"
        if any(p.startswith("pci") for p in parts):
            return "pci"
        return None

    def _is_up(self, name: str) -> bool:
        return _read_text(self._sys_path / "class" / "net" / name / "operstate") == "up"

    def _speed(self, name: str) -> int | None:
        # Reading speed on a down interface raises EINVAL in the kernel, which
        # surfaces as an empty read — not an error worth reporting.
        raw = _read_text(self._sys_path / "class" / "net" / name / "speed")
        try:
            value = int(raw)
        except ValueError:
            return None
        return value if value > 0 else None


class RdmaCollector(Collector[list[RdmaPort]]):
    """RDMA ports from /sys/class/infiniband.

    Covers both native InfiniBand and RoCE, since mlx5 registers RoCE devices
    in the same tree. Returns an empty list when the directory doesn't exist,
    which is the normal case on a node with no RDMA hardware.

    TRAFFIC COMES FROM THE NETDEV, NOT FROM counters/. Verified on the GX10:
    port_rcv_data and port_xmit_data read 0 on ports that are ACTIVE with
    LinkUp, because mlx5 doesn't populate the InfiniBand-style byte counters
    for an Ethernet link layer. Reading them would have reported 0 b/s forever
    on a working link — wrong, and wrong in a way that looks like an idle
    fabric rather than a bug.

    Each RoCE device is paired with its Ethernet interface through the shared
    PCI device (../device/net/), and the byte counters are read from there.
    """

    name = "rdma"

    def __init__(self, sys_path: Path) -> None:
        self._sys_path = sys_path
        self._root = sys_path / "class" / "infiniband"
        self._rates = RateTracker()

    def collect(self) -> list[RdmaPort]:
        if not self._root.is_dir():
            return []

        ports: list[RdmaPort] = []
        live_keys: set[str] = set()

        for device_dir in sorted(self._root.iterdir()):
            ports_dir = device_dir / "ports"
            if not ports_dir.is_dir():
                continue

            netdev = self._netdev_for(device_dir)

            for port_dir in sorted(ports_dir.iterdir()):
                try:
                    port_num = int(port_dir.name)
                except ValueError:
                    continue

                state = _strip_enum(_read_text(port_dir / "state"))
                active = state.upper().endswith("ACTIVE")

                rx_bytes, tx_bytes = self._netdev_bytes(netdev)
                key = f"{device_dir.name}:{port_num}"
                live_keys |= {f"{key}:rx", f"{key}:tx"}

                counters = port_dir / "counters"
                ports.append(
                    RdmaPort(
                        device=device_dir.name,
                        port=port_num,
                        state=state,
                        physical_state=_strip_enum(_read_text(port_dir / "phys_state")),
                        link_layer=_read_text(port_dir / "link_layer"),
                        # The driver reports a placeholder rate on a down port
                        # ("40 Gb/sec (4X QDR)" on hardware rated far higher),
                        # so it's only meaningful while the link is up. Showing
                        # it regardless would look like a negotiation fault.
                        rate=_read_text(port_dir / "rate") if active else "",
                        interface=netdev or "",
                        rx_bytes_per_sec=self._rates.rate(f"{key}:rx", rx_bytes),
                        tx_bytes_per_sec=self._rates.rate(f"{key}:tx", tx_bytes),
                        rx_bytes_total=rx_bytes,
                        tx_bytes_total=tx_bytes,
                        errors=(
                            _read_int(counters / "port_rcv_errors")
                            + _read_int(counters / "port_xmit_discards")
                            + _read_int(counters / "link_downed")
                        ),
                    )
                )

        self._rates.forget(live_keys)
        return ports

    def _netdev_for(self, device_dir: Path) -> str | None:
        """The Ethernet interface backing a RoCE device.

        Both hang off the same PCI function, so the RDMA device's `device`
        symlink has a `net/` directory naming the interface — that's the link
        between roceP2p1s0f0 and enP2p1s0f0np0.
        """
        net_dir = device_dir / "device" / "net"
        try:
            names = sorted(p.name for p in net_dir.iterdir())
        except OSError:
            return None
        return names[0] if names else None

    def _netdev_bytes(self, netdev: str | None) -> tuple[int, int]:
        if not netdev:
            return 0, 0
        stats = self._sys_path / "class" / "net" / netdev / "statistics"
        return _read_int(stats / "rx_bytes"), _read_int(stats / "tx_bytes")


def _read_text(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _read_int(path: Path) -> int:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return 0


def _strip_enum(value: str) -> str:
    """sysfs reports these as "4: ACTIVE" — keep the name, drop the ordinal."""
    return value.split(":", 1)[-1].strip() if ":" in value else value
