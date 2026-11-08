"""Dump raw hardware/runtime facts for debugging detection logic.

Run inside the agent container, where it has the same NVML access and PID
namespace view the agent does:

    docker run --rm --gpus all --pid host \\
      -v /proc:/host/proc:ro \\
      -e LLAMA_ROUTER_URLS=http://host:8080 \\
      spark-dash-agent:latest python -m spark_dash_agent.diagnose

With BACKEND_URL, NODE_ID and AGENT_TOKEN set as in the node stack, it asks
the backend for this node's routers and engines and their API keys instead, so
one started with an API key is read as the agent reads it.

Read-only. Notably it does NOT touch /metrics?model=X — that would autoload
sleeping models, which is the exact thing the agent exists to avoid.
"""

from __future__ import annotations

import json
import os
import sys

import httpx


def _section(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def dump_processes() -> None:
    """Show what NVML and /proc actually report for each GPU process.

    The point is to see *why* runtime inference succeeded or failed — whether
    the command line was readable at all, and what it contained.
    """
    _section("GPU PROCESSES — what runtime inference has to work with")
    try:
        from nvitop import NA, Device

        from spark_dash_agent.collectors.gpu import _command_line, _cwd, infer_runtime
    except Exception as exc:  # noqa: BLE001
        print(f"NVML unavailable: {type(exc).__name__}: {exc}")
        return

    try:
        device = Device(0)
        processes = device.processes()
    except Exception as exc:  # noqa: BLE001
        print(f"could not enumerate processes: {type(exc).__name__}: {exc}")
        return

    if not processes:
        print("no GPU processes running")
        return

    for pid, proc in sorted(processes.items()):
        print(f"\n--- pid {pid} ---")
        for label, attr in (("name", "name"), ("username", "username"), ("cwd", "cwd")):
            try:
                value = getattr(proc, attr)()
                print(f"  {label:9}: {value!r}")
            except Exception as exc:  # noqa: BLE001
                print(f"  {label:9}: <FAILED {type(exc).__name__}: {exc}>")

        # Both forms, separately — which one works tells us whether the empty
        # runtime label is a permissions problem or a genuine non-match.
        for attr in ("cmdline", "command"):
            try:
                value = getattr(proc, attr)()
                shown = value if value is not NA else "<NA>"
                print(f"  {attr:9}: {shown!r}")
            except Exception as exc:  # noqa: BLE001
                print(f"  {attr:9}: <FAILED {type(exc).__name__}: {exc}>")

        try:
            mem = proc.gpu_memory()
            print(f"  gpu_mem  : {mem}")
        except Exception as exc:  # noqa: BLE001
            print(f"  gpu_mem  : <FAILED {exc}>")

        resolved = _command_line(proc)
        cwd = _cwd(proc)
        try:
            name = str(proc.name())
        except Exception:  # noqa: BLE001
            name = ""
        print(f"  -> command line used : {resolved[:160]!r}")
        print(f"  -> cwd used          : {cwd!r}")
        print(f"  -> inferred runtime  : {infer_runtime(name, resolved, cwd)}")


def dump_routers(urls: list[str], keys: dict[str, str] | None = None) -> None:
    """Print each router's raw /v1/models payload.

    This is what the residency heuristic keys off. Safe to call: /v1/models
    takes no `model` parameter and cannot trigger an autoload.
    """
    _section("LLAMA.CPP ROUTERS — raw /v1/models payloads")
    if not urls:
        print("no routers configured (set LLAMA_ROUTER_URLS)")
        return

    for url in urls:
        base = url.rstrip("/")
        key = (keys or {}).get(base)
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        print(f"\n--- {base}/v1/models{' (with its API key)' if key else ''} ---")
        try:
            resp = httpx.get(f"{base}/v1/models", headers=headers, timeout=5.0)
            print(f"  HTTP {resp.status_code}")
            try:
                print(json.dumps(resp.json(), indent=2)[:4000])
            except ValueError:
                print(f"  non-JSON body: {resp.text[:500]!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"  UNREACHABLE: {type(exc).__name__}: {exc}")

        # Some builds expose richer state here. Deliberately called WITHOUT a
        # model parameter so it cannot wake anything.
        for path in ("/props", "/slots"):
            try:
                resp = httpx.get(f"{base}{path}", headers=headers, timeout=5.0)
                body = resp.text[:600]
                print(f"\n  {path} -> HTTP {resp.status_code}: {body!r}")
            except Exception as exc:  # noqa: BLE001
                print(f"  {path} -> {type(exc).__name__}")


def dump_engines(engines: dict[str, list[str]], keys: dict[str, str] | None = None) -> None:
    """What each vLLM, SGLang or TensorFold endpoint answers the agent: the
    scrape, and the model listing a TensorFold row is named from. Status codes
    only for the scrape, which is long."""
    _section("ENGINES — /metrics and /v1/models")
    if not any(engines.values()):
        print("no engines configured centrally")
        return

    for runtime, urls in sorted(engines.items()):
        for url in urls:
            key = (keys or {}).get(url.rstrip("/"))
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            base = url[: -len("/metrics")] if url.endswith("/metrics") else url.rstrip("/")
            print(f"\n--- {runtime} {base}{' (with its API key)' if key else ''} ---")
            for target, listing in ((url, False), (f"{base}/v1/models", True)):
                try:
                    resp = httpx.get(target, headers=headers, timeout=5.0)
                    body = resp.text[:300] if listing else f"{len(resp.text)} bytes"
                    print(f"  {target} -> HTTP {resp.status_code}: {body}")
                except Exception as exc:  # noqa: BLE001
                    print(f"  {target} -> UNREACHABLE: {type(exc).__name__}: {exc}")


def dump_network() -> None:
    """Raw network and RDMA sysfs, for confirming what this hardware exposes.

    The RDMA half especially: the GX10s cluster over ConnectX-7 running RoCEv2
    rather than native InfiniBand, so devices appear under /sys/class/infiniband
    with link_layer "Ethernet". Worth seeing the real tree rather than trusting
    that assumption.
    """
    import os
    from pathlib import Path

    _section("NETWORK INTERFACES")
    sys_path = Path(os.environ.get("SYS_PATH", "/sys"))
    net_root = sys_path / "class" / "net"
    if not net_root.is_dir():
        print(f"  no {net_root}")
    else:
        for iface in sorted(net_root.iterdir()):
            physical = (iface / "device").exists()
            state = _slurp(iface / "operstate")
            speed = _slurp(iface / "speed")
            mark = "physical" if physical else "virtual "
            print(f"  {mark}  {iface.name:20} state={state:8} speed={speed or '-'}")

    _section("RDMA / INFINIBAND")
    ib_root = sys_path / "class" / "infiniband"
    if not ib_root.is_dir():
        print(f"  no {ib_root} — no RDMA hardware, or the module isn't loaded")
        return

    for device in sorted(ib_root.iterdir()):
        print(f"\n--- {device.name} ---")
        for attr in ("node_type", "node_desc", "fw_ver", "hca_type"):
            value = _slurp(device / attr)
            if value:
                print(f"  {attr:12}: {value}")

        ports = device / "ports"
        if not ports.is_dir():
            continue
        for port in sorted(ports.iterdir()):
            print(f"  port {port.name}:")
            for attr in ("state", "phys_state", "link_layer", "rate"):
                print(f"    {attr:12}: {_slurp(port / attr)}")

            counters = port / "counters"
            if counters.is_dir():
                names = sorted(p.name for p in counters.iterdir())
                print(f"    counters    : {len(names)} available")
                for want in ("port_rcv_data", "port_xmit_data", "port_rcv_errors"):
                    print(f"      {want:18}= {_slurp(counters / want)}")
            # RoCE exposes extra counters here that native IB does not.
            hw = port / "hw_counters"
            if hw.is_dir():
                print(f"    hw_counters : {len(list(hw.iterdir()))} available (RoCE)")


def _slurp(path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def dump_device() -> None:
    _section("GPU DEVICE")
    try:
        from nvitop import Device

        device = Device(0)
        print(f"  name         : {device.name()}")
        print(f"  memory_total : {device.memory_total()}")
        print(f"  sm_clock     : {device.sm_clock()} MHz")
        print(f"  max_sm_clock : {device.max_sm_clock()} MHz")
        print(f"  power_draw   : {device.power_draw()} mW")
        print(f"  power_limit  : {device.power_limit()} mW")
        print(f"  temperature  : {device.temperature()} C")
    except Exception as exc:  # noqa: BLE001
        print(f"  unavailable: {type(exc).__name__}: {exc}")


def _central_config() -> tuple[list[str], dict[str, list[str]], dict[str, str]]:
    """The routers, engines and keys central config gives this node, as the
    agent sees them: BACKEND_URL, NODE_ID and AGENT_TOKEN from the same
    environment. Empty when there is no backend or the node is not in the
    cluster file."""
    try:
        import time

        from spark_dash_agent.config import Settings
        from spark_dash_agent.remote_config import RemoteConfig

        settings = Settings()
        remote = RemoteConfig(
            settings.backend_url,
            settings.resolve_node_id(),
            token=settings.agent_token,
            sealed=settings.agent_sealed_keys,
        )
        cfg = remote.current(time.monotonic())
    except Exception:  # noqa: BLE001 — diagnostics must still run
        return [], {}, {}
    return (cfg.llama_routers, cfg.engines, cfg.router_keys) if cfg else ([], {}, {})


def main() -> int:
    urls = [u.strip() for u in os.environ.get("LLAMA_ROUTER_URLS", "").split(",") if u.strip()]
    central, engines, keys = _central_config()
    dump_device()
    dump_network()
    dump_processes()
    dump_routers(central or urls, keys)
    dump_engines(engines, keys)
    print("\nPaste this output back to continue tuning detection.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
