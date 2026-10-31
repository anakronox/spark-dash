"""RDMA round-trip probe over each RoCE link (spike, roadmap CS).

The ConnectX keeps no latency counter, and ping over the RoCE addresses
measures the kernel stack: 0.11-1.1 ms against a 2-7 us RDMA round trip,
measured 2026-10-31. So the agent runs a real RDMA ping-pong, with
`ibv_rc_pingpong` from ibverbs-utils.

NOT PERFTEST. `ib_write_lat` spends 0.43 s of CPU per run calibrating the
clock, whatever the iteration count; `ibv_rc_pingpong` has no calibration and
ran 1000 round trips in 0.01 s.

HOW IT RUNS:

- A LISTENER per RDMA device: an `ibv_rc_pingpong` server on its own TCP port
  (PORT_BASE + the device's index), restarted after each client. It sits in
  accept() between probes and costs nothing.
- `/rdma/listeners` on the agent says which address is served on which port.
- A PROBER thread, every interval: for each RoCE port, its neighbours from the
  host's ARP table. A neighbour is probed only once its own agent has listed
  the address, so nothing is sent to a host that is not one of ours. A
  neighbour that is not an agent is asked again after an hour.

The TCP handshake goes to the peer's address on the link itself; the agent's
published port answers on every host address. RDMA uses the devices'
`netns shared` mode, so the agent stays on its bridge network.

OFF unless RDMA_PROBE=1. The dashboard otherwise only reads, and this sends
traffic on the fabric.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import random
import re
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

PINGPONG = "ibv_rc_pingpong"
#: Small enough to cost microseconds, large enough for a stable mean.
ITERATIONS = 100
SIZE = 64
#: A run that has not finished in this long is a stuck peer, not a slow link.
RUN_TIMEOUT_S = 5.0
#: A listener nobody connected to is restarted after this, so one that a
#: client abandoned mid-handshake cannot hold its port forever.
LISTENER_RECYCLE_S = 300.0
NOT_AN_AGENT_RETRY_S = 3600.0
AGENT_RETRY_S = 300.0

_RESULT = re.compile(r"(\d+) iters in [\d.]+ seconds = ([\d.]+) usec/iter")


@dataclass(frozen=True)
class Gid:
    device: str
    port: int
    index: int
    ipv4: str


@dataclass
class ProbeResult:
    device: str
    port: int
    peer: str
    peer_node: str
    rtt_us: float | None
    error: str = ""
    at: float = field(default_factory=time.time)


def roce_v2_ipv4_gids(sys_path: Path) -> list[Gid]:
    """One RoCE v2 IPv4 GID per active port, link-local last.

    A port can carry several addresses (sparkjr's enP2p1s0f1np1 has a
    169.254 one beside its real one); the routable one is what peers use.
    """
    root = sys_path / "class" / "infiniband"
    out: list[Gid] = []
    if not root.is_dir():
        return out
    for dev in sorted(root.iterdir()):
        for port_dir in sorted((dev / "ports").glob("*")):
            try:
                port = int(port_dir.name)
            except ValueError:
                continue
            found: list[Gid] = []
            for gid_file in (port_dir / "gids").glob("*"):
                try:
                    index = int(gid_file.name)
                    raw = gid_file.read_text().strip()
                    kind = (port_dir / "gid_attrs" / "types" / gid_file.name).read_text().strip()
                except (OSError, ValueError):
                    continue
                if kind != "RoCE v2" or not raw.startswith("0000:0000:0000:0000:0000:ffff:"):
                    continue
                hexed = raw.replace(":", "")[-8:]
                ip = str(ipaddress.IPv4Address(int(hexed, 16)))
                found.append(Gid(dev.name, port, index, ip))
            found.sort(key=lambda g: (ipaddress.IPv4Address(g.ipv4).is_link_local, g.index))
            if found:
                out.append(found[0])
    return out


def neighbours(proc_path: Path, netdev: str) -> list[str]:
    """Complete ARP entries on one interface, from the HOST's table.

    `/proc/1/net/arp` and not `/proc/net/arp`: the latter resolves through the
    reading process's own network namespace, which is the container's. With
    `pid: host`, pid 1 is the host's init.
    """
    try:
        lines = (proc_path / "1" / "net" / "arp").read_text().splitlines()[1:]
    except OSError:
        return []
    out = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 6 and parts[5] == netdev and parts[2] == "0x2":
            out.append(parts[0])
    return sorted(set(out))


def parse_rtt(output: str) -> float | None:
    """Microseconds per round trip, from the client's summary line."""
    m = _RESULT.search(output)
    return float(m.group(2)) if m else None


class RdmaProber:
    def __init__(
        self,
        sys_path: Path,
        proc_path: Path,
        node_id: str,
        *,
        port_base: int,
        peer_agent_port: int,
        interval_s: float,
    ) -> None:
        self._sys = sys_path
        self._proc = proc_path
        self._node_id = node_id
        self._port_base = port_base
        self._peer_agent_port = peer_agent_port
        self._interval_s = interval_s
        self._lock = threading.Lock()
        self._results: dict[tuple[str, int, str], ProbeResult] = {}
        self._peers: dict[str, tuple[float, dict | None]] = {}
        self._stop = threading.Event()
        self._gids: list[Gid] = []

    # --- listeners -----------------------------------------------------------

    def _device_ports(self) -> dict[str, int]:
        root = self._sys / "class" / "infiniband"
        names = sorted(p.name for p in root.iterdir()) if root.is_dir() else []
        return {name: self._port_base + i for i, name in enumerate(names)}

    def listeners(self) -> dict:
        """What `/rdma/listeners` serves: this node, and which address is
        answered on which TCP port."""
        ports = self._device_ports()
        return {
            "node": self._node_id,
            "listeners": {
                g.ipv4: {"device": g.device, "port": ports[g.device], "gid_index": g.index}
                for g in self._gids
                if g.device in ports
            },
        }

    def _listen_forever(self, gid: Gid, tcp_port: int) -> None:
        cmd = [PINGPONG, "-d", gid.device, "-i", str(gid.port), "-g", str(gid.index),
               "-p", str(tcp_port), "-n", str(ITERATIONS), "-s", str(SIZE)]
        while not self._stop.is_set():
            try:
                subprocess.run(cmd, capture_output=True, timeout=LISTENER_RECYCLE_S, check=False)
            except subprocess.TimeoutExpired:
                continue
            except OSError:
                log.warning("rdma probe: cannot run %s", PINGPONG, exc_info=True)
                self._stop.wait(60)

    # --- prober --------------------------------------------------------------

    def _peer_listeners(self, ip: str, now: float) -> dict | None:
        cached = self._peers.get(ip)
        if cached and now < cached[0]:
            return cached[1]
        info = None
        try:
            with urllib.request.urlopen(  # noqa: S310 — a fixed http URL to a neighbour
                f"http://{ip}:{self._peer_agent_port}/rdma/listeners", timeout=1.0
            ) as resp:
                info = json.load(resp)
        except Exception:  # noqa: BLE001 — anything but an answer means "not an agent"
            info = None
        ok = bool(info and ip in info.get("listeners", {}))
        self._peers[ip] = (now + (AGENT_RETRY_S if ok else NOT_AN_AGENT_RETRY_S), info if ok else None)
        return info if ok else None

    def _netdev(self, device: str) -> str | None:
        try:
            names = sorted(p.name for p in (self._sys / "class" / "infiniband" / device / "device" / "net").iterdir())
        except OSError:
            return None
        return names[0] if names else None

    def _probe(self, gid: Gid, peer_ip: str, info: dict) -> ProbeResult:
        target = info["listeners"][peer_ip]
        cmd = [PINGPONG, "-d", gid.device, "-i", str(gid.port), "-g", str(gid.index),
               "-p", str(target["port"]), "-n", str(ITERATIONS), "-s", str(SIZE), peer_ip]
        node = str(info.get("node", ""))
        try:
            run = subprocess.run(cmd, capture_output=True, text=True, timeout=RUN_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired:
            return ProbeResult(gid.device, gid.port, peer_ip, node, None, "timed out")
        rtt = parse_rtt(run.stdout)
        if rtt is None:
            # Refused is the usual case: another node is probing the same
            # listener this second. The next round tries again.
            err = (run.stderr.strip().splitlines() or ["no result"])[-1][:120]
            return ProbeResult(gid.device, gid.port, peer_ip, node, None, err)
        return ProbeResult(gid.device, gid.port, peer_ip, node, rtt)

    def _round(self) -> None:
        now = time.time()
        for gid in self._gids:
            netdev = self._netdev(gid.device)
            if not netdev:
                continue
            for ip in neighbours(self._proc, netdev):
                if ip == gid.ipv4:
                    continue
                info = self._peer_listeners(ip, now)
                if not info:
                    continue
                result = self._probe(gid, ip, info)
                with self._lock:
                    self._results[(gid.device, gid.port, ip)] = result

    def _probe_forever(self) -> None:
        while not self._stop.is_set():
            # Jittered, so two nodes probing the same listener through a
            # switch do not collide every round.
            if self._stop.wait(self._interval_s * random.uniform(0.8, 1.2)):  # noqa: S311
                return
            try:
                self._round()
            except Exception:  # noqa: BLE001 — a probe must never take the agent down
                log.warning("rdma probe round failed", exc_info=True)

    def results(self, max_age_s: float) -> list[ProbeResult]:
        cutoff = time.time() - max_age_s
        with self._lock:
            return [r for r in self._results.values() if r.at >= cutoff]

    def start(self) -> None:
        self._gids = roce_v2_ipv4_gids(self._sys)
        ports = self._device_ports()
        for gid in self._gids:
            threading.Thread(
                target=self._listen_forever, args=(gid, ports[gid.device]),
                name=f"rdma-listen-{gid.device}", daemon=True,
            ).start()
        threading.Thread(target=self._probe_forever, name="rdma-probe", daemon=True).start()
        log.info("rdma probe on: %s", ", ".join(f"{g.device}={g.ipv4}" for g in self._gids) or "no RoCE GIDs")
