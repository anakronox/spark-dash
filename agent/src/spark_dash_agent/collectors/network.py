"""Network interfaces and RDMA ports.

Two collectors that read sysfs and procfs directly rather than going through
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
    """Physical interfaces, with throughput derived between samples."""

    name = "network"

    def __init__(self, proc_path: Path, sys_path: Path) -> None:
        self._proc_path = proc_path
        self._sys_path = sys_path
        self._rates = RateTracker()

    def collect(self) -> list[NetworkInterface]:
        stats = _read_proc_net_dev(self._proc_path / "net" / "dev")
        if not stats:
            return []

        interfaces: list[NetworkInterface] = []
        live_keys: set[str] = set()

        for name, counters in sorted(stats.items()):
            if not self._is_physical(name):
                continue

            rx_key, tx_key = f"{name}:rx", f"{name}:tx"
            live_keys |= {rx_key, tx_key}

            interfaces.append(
                NetworkInterface(
                    name=name,
                    up=self._is_up(name),
                    speed_mbps=self._speed(name),
                    rx_bytes_per_sec=self._rates.rate(rx_key, counters["rx_bytes"]),
                    tx_bytes_per_sec=self._rates.rate(tx_key, counters["tx_bytes"]),
                    rx_bytes_total=int(counters["rx_bytes"]),
                    tx_bytes_total=int(counters["tx_bytes"]),
                    rx_errors=int(counters["rx_errs"]),
                    tx_errors=int(counters["tx_errs"]),
                    rx_dropped=int(counters["rx_drop"]),
                    tx_dropped=int(counters["tx_drop"]),
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
    """

    name = "rdma"

    def __init__(self, sys_path: Path) -> None:
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

            for port_dir in sorted(ports_dir.iterdir()):
                try:
                    port_num = int(port_dir.name)
                except ValueError:
                    continue

                counters = port_dir / "counters"
                rx_words = _read_int(counters / "port_rcv_data")
                tx_words = _read_int(counters / "port_xmit_data")

                # These counters are in 4-BYTE WORDS, not bytes — a detail
                # that silently under-reports throughput by 4x if missed.
                rx_bytes = rx_words * 4
                tx_bytes = tx_words * 4

                key = f"{device_dir.name}:{port_num}"
                live_keys |= {f"{key}:rx", f"{key}:tx"}

                ports.append(
                    RdmaPort(
                        device=device_dir.name,
                        port=port_num,
                        state=_strip_enum(_read_text(port_dir / "state")),
                        physical_state=_strip_enum(_read_text(port_dir / "phys_state")),
                        link_layer=_read_text(port_dir / "link_layer"),
                        rate=_read_text(port_dir / "rate"),
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


def _read_proc_net_dev(path: Path) -> dict[str, dict[str, int]]:
    """Parse /proc/net/dev.

    Format is two header lines then one row per interface:

        eth0: 12345 100 0 0 0 0 0 0  67890 200 0 0 0 0 0 0

    Columns are receive then transmit, each: bytes packets errs drop fifo
    frame compressed multicast.
    """
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {}

    out: dict[str, dict[str, int]] = {}
    for line in lines[2:]:
        name, _, rest = line.partition(":")
        fields = rest.split()
        if not name.strip() or len(fields) < 16:
            continue
        try:
            values = [int(f) for f in fields[:16]]
        except ValueError:
            continue

        out[name.strip()] = {
            "rx_bytes": values[0],
            "rx_packets": values[1],
            "rx_errs": values[2],
            "rx_drop": values[3],
            "tx_bytes": values[8],
            "tx_packets": values[9],
            "tx_errs": values[10],
            "tx_drop": values[11],
        }
    return out
