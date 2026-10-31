"""RDMA round-trip probe over each RoCE link (roadmap CS).

The ConnectX keeps no latency counter, and ping over the RoCE addresses
measures the kernel stack: 0.11-1.1 ms against a 6-7 us RDMA round trip,
measured 2026-10-31. So the agent runs a real RDMA ping-pong, with
`ibv_rc_pingpong` from ibverbs-utils.

NOT PERFTEST. `ib_write_lat` spends 0.43 s of CPU per run calibrating the
clock, whatever the iteration count; `ibv_rc_pingpong` has no calibration.
Measured in the agent: 100 round trips, 6 ms of CPU, 20 ms wall.

HOW IT RUNS:

- PEERS ARE PINNED in cluster.yml (`rdma_probe: {peers: [...]}`), the way an
  inference stack pins its NCCL peers, and nothing else is probed. A node
  with no peers listed runs no listener and no prober.
- A LISTENER per RDMA device: an `ibv_rc_pingpong` server on its own TCP port
  (PORT_BASE + the device's index), restarted after each client. It sits in
  accept() between probes: four of them held about 2 MB, measured.
- `/rdma/listeners` on the agent says which address is served on which port.
- A PROBER thread, every interval: each peer, from the local RoCE address
  closest to it.

The TCP handshake goes to the peer's address on the link itself; the agent's
published port answers on every host address. RDMA uses the devices'
`netns shared` mode, so the agent stays on its bridge network.
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
#: How long a peer's port list is trusted before asking again.
LISTENERS_TTL_S = 300.0

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
    """One RoCE v2 IPv4 GID per port, link-local last.

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


def closest(gids: list[Gid], peer: str, min_bits: int = 16) -> Gid | None:
    """The local address sharing the longest prefix with the peer.

    The kernel's own prefix length is in the host's network namespace, which
    the agent cannot read from its bridge network; the longest shared prefix
    picks the same port on every subnet here. Under 16 shared bits is no
    match: such a peer is not on any link this node has.
    """
    want = int(ipaddress.IPv4Address(peer))
    best, best_bits = None, min_bits - 1
    for g in gids:
        bits = 32 - (int(ipaddress.IPv4Address(g.ipv4)) ^ want).bit_length()
        if bits > best_bits:
            best, best_bits = g, bits
    return best


def parse_rtt(output: str) -> float | None:
    """Microseconds per round trip, from the client's summary line."""
    m = _RESULT.search(output)
    return float(m.group(2)) if m else None


class RdmaProber:
    """Listeners and prober, started and stopped by the peer list."""

    def __init__(
        self,
        sys_path: Path,
        node_id: str,
        *,
        port_base: int,
        peer_agent_port: int,
        interval_s: float,
    ) -> None:
        self._sys = sys_path
        self._node_id = node_id
        self._port_base = port_base
        self._peer_agent_port = peer_agent_port
        self._interval_s = interval_s
        self._lock = threading.Lock()
        self._results: dict[tuple[str, int, str], ProbeResult] = {}
        self._listeners_of: dict[str, tuple[float, dict]] = {}
        self._peers: tuple[str, ...] = ()
        self._gids: list[Gid] = []
        self._stop: threading.Event | None = None
        self._children: set[subprocess.Popen] = set()

    @property
    def active(self) -> bool:
        return self._stop is not None

    def configure(self, peers: list[str]) -> None:
        """Start, stop or retarget. Called on every snapshot; a no-op when the
        list has not changed."""
        wanted = tuple(sorted(set(peers)))
        if wanted == self._peers:
            return
        self._peers = wanted
        with self._lock:
            self._results = {k: v for k, v in self._results.items() if k[2] in wanted}
        if wanted and not self.active:
            self._start()
        elif not wanted and self.active:
            self._halt()

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

    def _run(self, cmd: list[str], timeout: float) -> tuple[str, str] | None:
        """One child process, killed on timeout or when the probe stops.
        None when it timed out."""
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)  # noqa: S603 — fixed binary, our own arguments
        with self._lock:
            self._children.add(proc)
        try:
            out, err = proc.communicate(timeout=timeout)
            return out, err
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return None
        finally:
            with self._lock:
                self._children.discard(proc)

    def _listen_forever(self, gid: Gid, tcp_port: int, stop: threading.Event) -> None:
        cmd = [PINGPONG, "-d", gid.device, "-i", str(gid.port), "-g", str(gid.index),
               "-p", str(tcp_port), "-n", str(ITERATIONS), "-s", str(SIZE)]
        while not stop.is_set():
            try:
                self._run(cmd, LISTENER_RECYCLE_S)
            except OSError:
                log.warning("rdma probe: cannot run %s", PINGPONG, exc_info=True)
                stop.wait(60)

    # --- prober --------------------------------------------------------------

    def _peer_listeners(self, ip: str, now: float) -> dict | None:
        cached = self._listeners_of.get(ip)
        if cached and now < cached[0]:
            return cached[1]
        try:
            with urllib.request.urlopen(  # noqa: S310 — a pinned peer, plain http on the LAN
                f"http://{ip}:{self._peer_agent_port}/rdma/listeners", timeout=1.0
            ) as resp:
                info = json.load(resp)
        except Exception:  # noqa: BLE001 — retried next round
            return None
        if ip not in info.get("listeners", {}):
            return None
        self._listeners_of[ip] = (now + LISTENERS_TTL_S, info)
        return info

    def _probe(self, peer: str, now: float) -> ProbeResult:
        gid = closest(self._gids, peer)
        if gid is None:
            return ProbeResult("", 0, peer, "", None, "no local RoCE address near this peer")
        info = self._peer_listeners(peer, now)
        if info is None:
            return ProbeResult(gid.device, gid.port, peer, "", None, "peer agent not listening")
        node = str(info.get("node", ""))
        cmd = [PINGPONG, "-d", gid.device, "-i", str(gid.port), "-g", str(gid.index),
               "-p", str(info["listeners"][peer]["port"]), "-n", str(ITERATIONS), "-s", str(SIZE), peer]
        ran = self._run(cmd, RUN_TIMEOUT_S)
        if ran is None:
            return ProbeResult(gid.device, gid.port, peer, node, None, "timed out")
        rtt = parse_rtt(ran[0])
        if rtt is None:
            # Refused is the usual case: another node is probing the same
            # listener this second. The next round tries again.
            err = (ran[1].strip().splitlines() or ["no result"])[-1][:120]
            return ProbeResult(gid.device, gid.port, peer, node, None, err)
        return ProbeResult(gid.device, gid.port, peer, node, rtt)

    def _probe_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            # Jittered, so two nodes probing one listener through a switch do
            # not collide every round.
            if stop.wait(self._interval_s * random.uniform(0.8, 1.2)):  # noqa: S311
                return
            now = time.time()
            for peer in self._peers:
                try:
                    result = self._probe(peer, now)
                except Exception:  # noqa: BLE001 — a probe must never take the agent down
                    log.warning("rdma probe to %s failed", peer, exc_info=True)
                    continue
                with self._lock:
                    self._results[(result.device, result.port, peer)] = result

    def results(self, max_age_s: float) -> list[ProbeResult]:
        cutoff = time.time() - max_age_s
        with self._lock:
            return [r for r in self._results.values() if r.at >= cutoff]

    def _start(self) -> None:
        stop = threading.Event()
        self._stop = stop
        self._gids = roce_v2_ipv4_gids(self._sys)
        ports = self._device_ports()
        for gid in self._gids:
            threading.Thread(
                target=self._listen_forever, args=(gid, ports[gid.device], stop),
                name=f"rdma-listen-{gid.device}", daemon=True,
            ).start()
        threading.Thread(target=self._probe_forever, args=(stop,), name="rdma-probe", daemon=True).start()
        log.info(
            "rdma probe on, peers %s; listening on %s",
            ", ".join(self._peers),
            ", ".join(f"{g.device}={g.ipv4}" for g in self._gids) or "nothing (no RoCE v2 IPv4 GIDs)",
        )

    def _halt(self) -> None:
        if self._stop is not None:
            self._stop.set()
        self._stop = None
        with self._lock:
            for proc in list(self._children):
                proc.kill()
            self._results.clear()
        self._gids = []
        log.info("rdma probe off: no peers configured")
