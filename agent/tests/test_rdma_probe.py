"""The RDMA probe's pure parts (CS): which address a port probes from,
who its neighbours are, and what the client printed."""

from pathlib import Path

from spark_dash_agent.collectors.rdma_probe import (
    RdmaProber,
    closest,
    parse_rtt,
    roce_v2_ipv4_gids,
)


def _gid(port: Path, index: int, raw: str, kind: str) -> None:
    (port / "gids").mkdir(parents=True, exist_ok=True)
    (port / "gid_attrs" / "types").mkdir(parents=True, exist_ok=True)
    (port / "gids" / str(index)).write_text(raw + "\n")
    (port / "gid_attrs" / "types" / str(index)).write_text(kind + "\n")


def _sys(tmp_path: Path) -> Path:
    ib = tmp_path / "class" / "infiniband"
    a = ib / "rocep1s0f0" / "ports" / "1"
    _gid(a, 0, "fe80:0000:0000:0000:32c5:99ff:fe40:878e", "IB/RoCE v1")
    _gid(a, 2, "0000:0000:0000:0000:0000:ffff:0a64:8801", "IB/RoCE v1")
    _gid(a, 3, "0000:0000:0000:0000:0000:ffff:0a64:8801", "RoCE v2")
    # Link-local AND routable on one port, as on sparkjr's enP2p1s0f1np1.
    b = ib / "roceP2p1s0f1" / "ports" / "1"
    _gid(b, 3, "0000:0000:0000:0000:0000:ffff:a9fe:8354", "RoCE v2")
    _gid(b, 5, "0000:0000:0000:0000:0000:ffff:c0a8:373f", "RoCE v2")
    # No IPv4 at all.
    _gid(ib / "mlx5_9" / "ports" / "1", 0, "fe80:0000:0000:0000:0000:0000:0000:0001", "RoCE v2")
    return tmp_path


def test_the_v2_ipv4_gid_is_chosen_and_routable_beats_link_local(tmp_path):
    gids = {g.device: (g.index, g.ipv4) for g in roce_v2_ipv4_gids(_sys(tmp_path))}
    assert gids == {"rocep1s0f0": (3, "10.100.136.1"), "roceP2p1s0f1": (5, "192.168.55.63")}


def test_a_peer_is_probed_from_the_local_address_nearest_it(tmp_path):
    gids = roce_v2_ipv4_gids(_sys(tmp_path))
    assert closest(gids, "10.100.136.2").device == "rocep1s0f0"
    assert closest(gids, "192.168.55.62").device == "roceP2p1s0f1"
    # Not on any link this node has.
    assert closest(gids, "172.16.0.9") is None


def test_no_peers_means_no_probe_and_listing_peers_starts_it(tmp_path, monkeypatch):
    prober = RdmaProber(_sys(tmp_path), "sparkjr", port_base=18515, peer_agent_port=9500, interval_s=30)
    started = []
    monkeypatch.setattr(prober, "_start", lambda: started.append(1) or setattr(prober, "_stop", object()))
    monkeypatch.setattr(prober, "_halt", lambda: started.append(0) or setattr(prober, "_stop", None))
    prober.configure([])
    assert not prober.active and started == []
    prober.configure(["10.100.136.2"])
    prober.configure(["10.100.136.2"])
    assert prober.active and started == [1]
    prober.configure([])
    assert not prober.active and started == [1, 0]


def test_the_client_summary_line_is_microseconds_per_round_trip():
    out = "  local address: ...\n128000 bytes in 0.01 seconds = 150.39 Mbit/sec\n1000 iters in 0.01 seconds = 6.81 usec/iter\n"
    assert parse_rtt(out) == 6.81
    assert parse_rtt("Couldn't connect to 10.100.136.2:18515") is None


def test_listeners_name_each_address_and_its_port(tmp_path):
    prober = RdmaProber(_sys(tmp_path), "sparkjr", port_base=18515, peer_agent_port=9500, interval_s=30)
    prober._gids = roce_v2_ipv4_gids(tmp_path)
    got = prober.listeners()
    assert got["node"] == "sparkjr"
    # Ports follow the sorted device list (mlx5_9, roceP2p1s0f1, rocep1s0f0:
    # uppercase sorts first), the same answer on every call.
    assert got["listeners"]["10.100.136.1"] == {"device": "rocep1s0f0", "port": 18517, "gid_index": 3}
    assert got["listeners"]["192.168.55.63"]["port"] == 18516
