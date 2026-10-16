"""Tools for agents: the dashboard as an MCP server (roadmap BB).

Ask (AY) sends a model one fixed bundle and it answers from that alone. These
tools let a model ask for what it needs instead: a finer step around the
moment something broke, a series the bundle leaves out, last week's same hour
to compare. The same tools serve two callers: an outside agent over MCP at
`/mcp`, and Ask itself when its model can call tools (BB4).

WHAT A TOOL CAN DO: read. Every tool here is a read the page already makes.
None silences an alert, opens a maintenance window or touches the fleet.

METRICS BY NAME. A metric is a key into `HISTORY_QUERIES`, as `/api/history`
takes it, so a tool call cannot become an arbitrary Prometheus query. Raw
PromQL is one tool of its own, behind a second switch (BB5).

CAPPED. Every list and every series has a ceiling and the result says when it
was cut, so one call cannot fill a small model's context.

WHERE THINGS LIVE. The switches are an `mcp:` block in the cluster file. The
file is served to the browser, so the key is a file of its own beside it,
`mcp.key`, mode 0600, as Ask's key is.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import yaml
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import Field

from .alert_history import fetch_episodes
from .annotations import fetch_annotations
from .cluster import (
    ClusterConfigError,
    NoClusterFileError,
    _atomic_write,
    _current_document,
    dump_cluster,
    parse_cluster,
)
from .prometheus import HISTORY_QUERIES, PrometheusClient, PrometheusError, rate_window

KEY_FILE = "mcp.key"

#: The ceilings. A series is downsampled to at most MAX_POINTS by widening the
#: step; lists past their ceiling are cut and say so.
MAX_POINTS = 240
MAX_SERIES = 40
MAX_ROWS = 60
#: How far back a tool may look: Prometheus keeps 180 days here, and a range
#: wider than a month at 240 points says little a summary would not.
MAX_MINUTES = 60 * 24 * 31
#: Prometheus's retention, for `end`.
RETENTION_S = 180 * 86400

# --- what each named series is -----------------------------------------------

#: name -> (unit, what it is). Pinned to HISTORY_QUERIES by a test, so a series added there is described
#: here or the suite fails.
METRICS: dict[str, tuple[str, str]] = {
    "gpu_utilization": ("%", "GPU busy"),
    "gpu_temperature": ("°C", "GPU temperature; the node's warning and critical bands are in cluster_now"),
    "gpu_power": ("W", "GPU power draw (the GPU alone; NVML on GB10 reports no whole-module figure)"),
    "gpu_clock": ("MHz", "GPU clock; GB10 targets about 3000 MHz under load and drops when throttling"),
    "memory_used_percent": ("%", "unified memory used; the GPU and CPU share this one pool"),
    "memory_used_bytes": ("bytes", "unified memory used"),
    "cpu_utilization": ("%", "CPU busy, all cores"),
    "cpu_clock": ("MHz", "CPU clock, averaged across cores, so throttling shows as a drop"),
    "psi_some_avg10": ("%", "memory pressure, some: share of time at least one task waited on memory (10 s average)"),
    "psi_full_avg10": ("%", "memory pressure, full: share of time every task waited on memory; nothing progressed"),
    "psi_cpu_some": ("%", "CPU pressure: share of time tasks waited for a CPU"),
    "psi_io_some": ("%", "I/O pressure: share of time tasks waited on I/O"),
    "swap_io": ("pages/s", "swap traffic in plus out; thrashing is a rate, not occupancy"),
    "disk_busy": ("%", "time the busiest disk was busy"),
    "tokens_per_second": ("tok/s", "decode throughput, summed over every engine on the node"),
    "prompt_tokens_per_second": ("tok/s", "prefill throughput, summed over every engine on the node"),
    "system_temperature": ("°C", "the hottest sensor on the node"),
    "package_temperature": ("°C", "the hottest CPU package zone"),
    "storage_temperature": ("°C", "the hottest NVMe drive"),
    "network_temperature": ("°C", "the hottest NIC"),
    "temperature_headroom": ("°C", "degrees left before the sensor closest to its limit reaches it"),
    "network_rx_bits": ("bit/s", "received, per interface"),
    "network_tx_bits": ("bit/s", "sent, per interface"),
    "network_errors": ("count", "receive plus transmit errors per step, per interface (usually physical)"),
    "network_drops": ("count", "receive plus transmit drops per step, per interface (usually backpressure)"),
    "network_link_up": ("0/1", "1 if the interface was up for the whole step"),
    "rdma_port_state": ("0/1", "1 if the RDMA port was ACTIVE for the whole step"),
    "monitoring_bytes": ("bytes", "resident memory of Prometheus, Alertmanager and the monitoring host's exporter; no node"),
    "gpu_workload_sm": ("%", "share of GPU compute (SM) time per workload: per node, runtime (llama.cpp, vllm, "
                             "comfyui, ...; 'unrecognised' for the rest) and model. Says WHAT was using the GPU "
                             "at a past time; the live process table only says it now"),
    "gpu_workload_memory_bytes": ("bytes", "GPU (unified) memory held per workload: per node, runtime and model"),
}

#: Excluded from `correlate`: not a property of one node.
NOT_PER_NODE = frozenset({"monitoring_bytes"})


# --- times --------------------------------------------------------------------

def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(raw: str | None, *, now: float | None = None) -> float:
    """ISO 8601 (`2026-10-16T21:00Z`, `2026-10-16 21:00`; no zone is UTC),
    `now`, or Unix seconds. Clamped to now; refused before retention."""
    now = time.time() if now is None else now
    if raw is None or not str(raw).strip() or str(raw).strip().lower() == "now":
        return now
    text = str(raw).strip()
    try:
        ts = float(text)
    except ValueError:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError as exc:
            raise ToolError(f"could not read the time {text!r}; give ISO 8601 UTC, like 2026-10-16T21:00Z") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        ts = dt.timestamp()
    if ts < now - RETENTION_S:
        raise ToolError(f"{text} is before the oldest data kept (180 days)")
    return min(ts, now)


def _window(minutes: int, end: str | None) -> tuple[float, float]:
    if minutes < 1 or minutes > MAX_MINUTES:
        raise ToolError(f"minutes must be 1 to {MAX_MINUTES} (31 days)")
    e = parse_time(end)
    return e - minutes * 60, e


def step_for(seconds: float, points: int = MAX_POINTS) -> int:
    """The finest step that keeps a range at or under `points`, never below
    the 15 s scrape, rounded to a whole 15 s."""
    raw = max(15.0, seconds / points)
    return int(math.ceil(raw / 15.0) * 15)


def _num(v: float) -> float | None:
    if v is None or math.isnan(v) or math.isinf(v):
        return None
    return round(v, 3) if abs(v) < 1000 else round(v, 1)


def _key(labels: dict) -> dict:
    """The labels that tell one series from another, in a stable order."""
    return {k: labels[k] for k in ("node", "runtime", "model", "interface", "device", "port") if labels.get(k)}


# --- the sources a tool reads -------------------------------------------------

@dataclass
class Sources:
    """What the tools read, handed in by the app, so this module never
    reaches into it and a test can pass fakes."""

    prom: PrometheusClient
    #: The live snapshot's nodes.
    nodes: Callable[[], Awaitable[list]]
    #: The header's time to first token, as `/api/latency` returns it.
    latency: Callable[[], Awaitable[dict]]
    #: The Clients card's rows for a window in minutes; None when client
    #: stats are off.
    clients: Callable[[int], Awaitable[dict | None]]
    #: Whether the raw PromQL tool is switched on, read per call.
    promql_enabled: Callable[[], bool]


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def gib(b) -> float | None:
    return round(b / 2**30, 1) if isinstance(b, (int, float)) else None


def _node(n) -> dict:
    d = n.model_dump(mode="json")
    g, m, c, p = d.get("gpu") or {}, d.get("memory") or {}, d.get("cpu") or {}, d.get("psi") or {}
    out: dict[str, Any] = {
        "node": d["node_id"],
        "cluster": d.get("cluster"),
        "up": d.get("up"),
        "health": d.get("health"),
        "health_reasons": d.get("health_reasons") or [],
        "gpu": {k: g.get(k) for k in ("util_pct", "temp_c", "power_w", "clock_mhz", "clock_state")},
        "memory_gib": {"used": gib(m.get("used_bytes")), "available": gib(m.get("available_bytes")),
                       "total": gib(m.get("total_bytes")), "swap_used": gib(m.get("swap_used_bytes"))},
        "cpu": {"util_pct": c.get("util_pct"), "temp_c": c.get("temp_c")},
        "memory_pressure": {"state": p.get("state"), "some_avg10": p.get("some_avg10"),
                            "full_avg10": p.get("full_avg10")},
    }
    bands = d.get("temp_bands")
    if bands:
        out["temperature_bands_c"] = {"gpu": [bands.get("gpu_warning_c"), bands.get("gpu_critical_c")],
                                      "cpu_package": [bands.get("cpu_warning_c"), bands.get("cpu_critical_c")]}
    if d.get("maintenance"):
        mt = d["maintenance"]
        out["maintenance"] = {"reason": mt.get("reason"), "ends_at": mt.get("ends_at")}
    procs = sorted(d.get("processes") or [], key=lambda x: -(x.get("gpu_mem_bytes") or 0))
    out["gpu_processes"] = [
        {k: v for k, v in {"name": x.get("name"), "pid": x.get("pid"), "runtime": x.get("runtime"),
                           "model": x.get("model"), "gpu_mem_gib": gib(x.get("gpu_mem_bytes"))}.items() if v is not None}
        for x in procs[:8]
    ]
    engines = []
    rt = d.get("runtimes") or {}
    for router in rt.get("llama_cpp") or []:
        engines.append({
            "runtime": "llama.cpp", "name": router.get("name") or router.get("endpoint"),
            "reachable": router.get("reachable"),
            "models": [{"name": x["name"], "state": x.get("state"), "requests_running": x.get("requests_running"),
                        "requests_waiting": x.get("requests_waiting"),
                        "generation_tok_s": x.get("generation_tokens_per_sec")}
                       for x in router.get("models") or [] if x.get("state") in ("loaded", "sleeping", "loading")],
        })
    for runtime in ("vllm", "sglang", "tensorfold"):
        for e in rt.get(runtime) or []:
            engines.append({"runtime": runtime, "model": e.get("model"), "reachable": e.get("reachable"),
                            "requests_running": e.get("requests_running"),
                            "requests_waiting": e.get("requests_waiting"),
                            "generation_tok_s": e.get("generation_tokens_per_sec"),
                            "kv_cache_pct": e.get("kv_cache_pct")})
    out["engines"] = engines
    return out


# --- the tools ----------------------------------------------------------------

async def cluster_now(src: Sources) -> dict:
    nodes = await src.nodes()
    try:
        firing = await src.prom.query('ALERTS{alertstate="firing"}')
    except PrometheusError:
        firing = None
    return {
        "now": iso(time.time()),
        "nodes": [_node(n) for n in nodes],
        "alerts_firing": None if firing is None else [
            {"alert": s.labels.get("alertname"), "severity": s.labels.get("severity"),
             "node": s.labels.get("node")} for s in firing
        ],
    }


def list_metrics() -> dict:
    return {
        "metrics": [{"name": k, "unit": u, "means": what} for k, (u, what) in METRICS.items()],
        "note": "Every series has a `node` label except monitoring_bytes; network_* and "
                "rdma_port_state are per interface. Times are UTC.",
    }


def _summary(points: list[tuple[float, float]]) -> dict | None:
    pts = [(t, v) for t, v in points if v is not None and not math.isnan(v)]
    if not pts:
        return None
    values = [v for _, v in pts]
    peak_t, peak = max(pts, key=lambda tv: tv[1])
    low_t, low = min(pts, key=lambda tv: tv[1])
    return {"min": _num(low), "min_at": iso(low_t), "avg": _num(sum(values) / len(values)),
            "max": _num(peak), "max_at": iso(peak_t), "first": _num(values[0]), "last": _num(values[-1])}


async def _range(src: Sources, metric: str, start: float, end: float, step_s: int):
    expr = HISTORY_QUERIES.get(metric)
    if expr is None:
        raise ToolError(f"unknown metric {metric!r}; list_metrics names them")
    expr = expr.replace("{window}", rate_window(f"{step_s}s"))
    try:
        return await src.prom.query_range(expr, start, end, f"{step_s}s")
    except PrometheusError as exc:
        raise ToolError(f"Prometheus did not answer: {exc}") from exc


async def metric_history(src: Sources, metric: str, minutes: int = 60, end: str | None = None,
                         nodes: list[str] | None = None, points: bool = False) -> dict:
    start, stop = _window(minutes, end)
    step = step_for(stop - start)
    series = await _range(src, metric, start, stop, step)
    if nodes:
        wanted = set(nodes)
        series = [s for s in series if s.node in wanted]
    series = sorted(series, key=lambda s: tuple(_key(s.labels).values()))
    unit, _ = METRICS.get(metric, ("", ""))
    out = []
    for s in series[:MAX_SERIES]:
        row: dict[str, Any] = {**_key(s.labels), "summary": _summary(s.points)}
        if points:
            # On the step grid from `start`, with null for a step that has no
            # sample, so the index alone gives the time.
            grid: dict[int, float] = {round((t - start) / step): v for t, v in s.points}
            n = int((stop - start) // step) + 1
            row["values"] = [_num(grid[i]) if i in grid else None for i in range(n)]
        out.append(row)
    result: dict[str, Any] = {"metric": metric, "unit": unit, "start": iso(start), "end": iso(stop),
                              "step_s": step, "series": out}
    if points:
        result["values_are"] = "one per step from start; null where nothing was recorded"
    if len(series) > MAX_SERIES:
        result["truncated"] = f"{len(series)} series, first {MAX_SERIES} shown; pass nodes to narrow"
    return result


def _step_str(minutes: int) -> str:
    return f"{max(60, step_for(minutes * 60, 600))}s"


async def alerts(src: Sources, minutes: int = 60, end: str | None = None) -> dict:
    start, stop = _window(minutes, end)
    try:
        episodes = await fetch_episodes(src.prom, start=start, end=stop, step=_step_str(minutes))
    except PrometheusError as exc:
        raise ToolError(f"Prometheus did not answer: {exc}") from exc
    rows = [
        {"alert": e.alertname, "severity": e.severity, "node": e.node, "started": iso(e.started_at),
         "fired": iso(e.fired_at) if e.fired_at else None,
         "ended": None if e.ongoing else iso(e.ended_at), "ongoing": e.ongoing}
        for e in episodes
    ]
    result: dict[str, Any] = {"start": iso(start), "end": iso(stop), "episodes": rows[-MAX_ROWS:],
                              "note": "fired null means it was pending and cleared before its for: period"}
    if len(rows) > MAX_ROWS:
        result["truncated"] = f"{len(rows)} episodes, the latest {MAX_ROWS} shown"
    return result


async def events(src: Sources, minutes: int = 60, end: str | None = None, node: str | None = None) -> dict:
    start, stop = _window(minutes, end)
    notes = await fetch_annotations(src.prom, start=start, end=stop, step=_step_str(minutes))
    if node:
        notes = [a for a in notes if a.node in (node, None)]
    rows = [{k: v for k, v in {"at": iso(a.ts), "kind": a.kind, "what": a.label, "node": a.node,
                               "until": iso(a.end_ts) if a.end_ts else None}.items() if v is not None}
            for a in notes]
    result: dict[str, Any] = {"start": iso(start), "end": iso(stop), "events": rows[-MAX_ROWS:]}
    if len(rows) > MAX_ROWS:
        result["truncated"] = f"{len(rows)} events, the latest {MAX_ROWS} shown"
    return result


async def latency(src: Sources) -> dict:
    try:
        body = await src.latency()
    except PrometheusError as exc:
        raise ToolError(f"Prometheus did not answer: {exc}") from exc
    return body


async def clients(src: Sources, minutes: int = 60) -> dict:
    if minutes < 5 or minutes > MAX_MINUTES:
        raise ToolError(f"minutes must be 5 to {MAX_MINUTES}")
    body = await src.clients(minutes)
    if body is None:
        return {"configured": False,
                "note": "client stats are off: the dashboard has no gateway in front of the engines"}
    rows = body.get("rows", [])
    out = {"configured": True, **{k: v for k, v in body.items() if k != "rows"}, "rows": rows[:MAX_ROWS]}
    if len(rows) > MAX_ROWS:
        out["truncated"] = f"{len(rows)} rows, the busiest {MAX_ROWS} shown"
    return out


#: How much a series must move, in units of its own spread before, to count as
#: moved; and the floor on that spread, as a share of its level, so a series
#: that sat perfectly flat does not turn a tiny wobble into a huge score.
MOVED = 3.0
FLOOR_SHARE = 0.05
#: And an absolute floor per unit, the smallest change that means anything.
#: A series sitting at zero has no spread and no level, so without this
#: memory pressure going 0.000 -> 0.015% scored 246 and headed the ranking
#: above ComfyUI taking the GPU from 0 to 58% (measured 2026-10-16).
UNIT_FLOOR: dict[str, float] = {
    "%": 0.5, "°C": 0.5, "W": 1.0, "MHz": 20.0, "bytes": 64 * 2**20, "pages/s": 1.0,
    "tok/s": 1.0, "bit/s": 1e5, "count": 1.0, "0/1": 0.5,
}


def _stats(values: list[float]) -> tuple[float, float]:
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return mean, math.sqrt(var)


async def correlate(src: Sources, node: str, at: str, minutes: int = 10) -> dict:
    """Rank what moved around `at`: the span is `minutes` either side, the
    baseline the `minutes * 3` before it. Every named series, on every node,
    so a cluster-wide change reads as one."""
    if minutes < 2 or minutes > 240:
        raise ToolError("minutes must be 2 to 240")
    now = time.time()
    centre = parse_time(at, now=now)
    span = (centre - minutes * 60, min(centre + minutes * 60, now))
    base = (span[0] - minutes * 180, span[0])
    step = step_for(span[1] - base[0], 160)

    metrics = [k for k in HISTORY_QUERIES if k not in NOT_PER_NODE]
    answers = await asyncio.gather(*(_range(src, m, base[0], span[1], step) for m in metrics),
                                   return_exceptions=True)
    rows = []
    unanswered = []
    for metric, series in zip(metrics, answers, strict=True):
        if isinstance(series, BaseException):
            unanswered.append(metric)
            continue
        for s in series:
            before = [v for t, v in s.points if t < base[1] and not math.isnan(v)]
            during = [(t, v) for t, v in s.points if t >= span[0] and not math.isnan(v)]
            if len(before) < 3 or not during:
                continue
            b_mean, b_sd = _stats(before)
            d_vals = [v for _, v in during]
            d_mean = sum(d_vals) / len(d_vals)
            # The point furthest from the baseline, up or down: a dip matters
            # as much as a spike (a clock falling, a link going to 0).
            far_t, far = max(during, key=lambda tv: abs(tv[1] - b_mean))
            unit = METRICS.get(metric, ("", ""))[0]
            floor = max(b_sd, FLOOR_SHARE * max(abs(b_mean), abs(d_mean)), UNIT_FLOOR.get(unit, 1e-9))
            score = abs(far - b_mean) / floor
            shift = abs(d_mean - b_mean) / floor
            rows.append({
                **_key(s.labels), "metric": metric, "unit": METRICS.get(metric, ("", ""))[0],
                "before_avg": _num(b_mean), "during_avg": _num(d_mean),
                "furthest": _num(far), "furthest_at": iso(far_t),
                "score": round(max(score, shift), 1),
            })
    rows.sort(key=lambda r: -r["score"])
    moved = [r for r in rows if r["score"] >= MOVED]

    # Which metrics moved on this node, and whether they moved elsewhere too.
    on_node = [r for r in moved if r.get("node") == node][:20]
    elsewhere: dict[str, list[str]] = {}
    for r in moved:
        if r.get("node") != node:
            elsewhere.setdefault(r["metric"], [])
            if r.get("node") and r["node"] not in elsewhere[r["metric"]]:
                elsewhere[r["metric"]].append(r["node"])
    for r in on_node:
        r["also_moved_on"] = elsewhere.get(r["metric"], [])
    others = [r for r in moved if r.get("node") != node and r["metric"] not in {x["metric"] for x in on_node}][:10]

    alerts_in = await alerts(src, minutes=max(1, int((span[1] - base[0]) / 60)) + 1, end=iso(span[1]))
    events_in = await events(src, minutes=max(1, int((span[1] - base[0]) / 60)) + 1, end=iso(span[1]))
    return {
        "node": node, "at": iso(centre),
        "span": [iso(span[0]), iso(span[1])], "baseline": [iso(base[0]), iso(base[1])], "step_s": step,
        "how": f"score is how far a series went from its baseline average, in baseline standard deviations "
               f"(floored at {int(FLOOR_SHARE * 100)}% of its level, and at a least meaningful change per "
               f"unit: 0.5 for % and °C, 1 W, 64 MiB); {MOVED:g} or more counts as moved",
        "moved_on_node": on_node,
        "moved_only_elsewhere": others,
        "alerts": alerts_in["episodes"],
        "events": events_in["events"],
        **({"unanswered": unanswered} if unanswered else {}),
    }


async def promql(src: Sources, query: str, minutes: int = 0, end: str | None = None) -> dict:
    if not src.promql_enabled():
        raise ToolError("raw PromQL is switched off in Settings; use metric_history")
    if not query.strip():
        raise ToolError("give a PromQL expression")
    try:
        if minutes <= 0:
            at = parse_time(end)
            series = await src.prom.query(query, at=at)
            step = None
        else:
            start, stop = _window(minutes, end)
            step = step_for(stop - start)
            series = await src.prom.query_range(query, start, stop, f"{step}s")
    except PrometheusError as exc:
        raise ToolError(f"Prometheus refused it: {exc}") from exc
    out = []
    for s in series[:MAX_SERIES]:
        if step is None:
            out.append({"labels": s.labels, "value": _num(s.points[-1][1]) if s.points else None,
                        "at": iso(s.points[-1][0]) if s.points else None})
        else:
            out.append({"labels": s.labels, "summary": _summary(s.points),
                        "points": [[iso(t), _num(v)] for t, v in s.points[-MAX_POINTS:]]})
    result: dict[str, Any] = {"query": query, "step_s": step, "series": out}
    if len(series) > MAX_SERIES:
        result["truncated"] = f"{len(series)} series, first {MAX_SERIES} shown; aggregate to narrow"
    return result


# --- the server ---------------------------------------------------------------

INSTRUCTIONS = (
    "Read-only access to the monitoring of a small cluster of NVIDIA GB10 (DGX Spark) machines: "
    "live state, every named series in Prometheus, alert episodes, events (deploys, model loads, "
    "cold starts, maintenance), time to first token, and which clients called the engines. "
    "Start with cluster_now and list_metrics. To explain a slowdown or a spike, call correlate "
    "with the node and the time, then metric_history for detail. To say what was using the GPU at "
    "a past time, read metric_history for gpu_workload_sm (compute share) and "
    "gpu_workload_memory_bytes, per runtime and model; cluster_now's process list is only the "
    "present. All times are UTC. GB10 has "
    "unified memory: the GPU and CPU share one pool, so memory pressure affects both."
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                            open_world_hint=False)

Minutes = Annotated[int, Field(description="how far back from `end`, in minutes (max 44640, 31 days)")]
End = Annotated[str | None, Field(description="the window's end, ISO 8601 UTC like 2026-10-16T21:00Z; "
                                              "default now")]


class DashboardMCP(MCPServer):
    """An MCPServer that lists `promql` only while its switch is on."""

    def __init__(self, src: Sources, **kwargs):
        super().__init__(**kwargs)
        self._src = src

    async def list_tools(self):
        tools = await super().list_tools()
        if self._src.promql_enabled():
            return tools
        return [t for t in tools if t.name != "promql"]


def build_server(src: Sources, version: str = "") -> DashboardMCP:
    server = DashboardMCP(src, name="spark-dash", title="spark-dash", version=version,
                          instructions=INSTRUCTIONS)

    async def t_cluster_now() -> str:
        return _dump(await cluster_now(src))

    async def t_list_metrics() -> str:
        return _dump(list_metrics())

    async def t_metric_history(
        metric: Annotated[str, Field(description="a name from list_metrics, like gpu_temperature")],
        minutes: Minutes = 60,
        end: End = None,
        nodes: Annotated[list[str] | None, Field(description="only these nodes; default all")] = None,
        points: Annotated[bool, Field(description="include the values, at most 240 per series; "
                                                  "default a summary only")] = False,
    ) -> str:
        return _dump(await metric_history(src, metric, minutes, end, nodes, points))

    async def t_alerts(minutes: Minutes = 60, end: End = None) -> str:
        return _dump(await alerts(src, minutes, end))

    async def t_events(minutes: Minutes = 60, end: End = None,
                       node: Annotated[str | None, Field(description="only this node's events, "
                                                                     "and the cluster's")] = None) -> str:
        return _dump(await events(src, minutes, end, node))

    async def t_latency() -> str:
        return _dump(await latency(src))

    async def t_clients(minutes: Annotated[int, Field(description="the window, in minutes (5 or more)")] = 60) -> str:
        return _dump(await clients(src, minutes))

    async def t_correlate(
        node: Annotated[str, Field(description="the node the problem was seen on")],
        at: Annotated[str, Field(description="when, ISO 8601 UTC like 2026-10-16T21:00Z")],
        minutes: Annotated[int, Field(description="half-width of the span around `at`, 2 to 240")] = 10,
    ) -> str:
        return _dump(await correlate(src, node, at, minutes))

    async def t_promql(
        query: Annotated[str, Field(description="a PromQL expression")],
        minutes: Annotated[int, Field(description="0 for an instant query at `end`; otherwise a range "
                                                  "this many minutes back, at most 240 points")] = 0,
        end: End = None,
    ) -> str:
        return _dump(await promql(src, query, minutes, end))

    tools = [
        (t_cluster_now, "cluster_now", "Each node as it stands now: health and why, GPU, memory, CPU, "
         "memory pressure, temperature bands, largest GPU processes, engines and their loaded models; "
         "and the alerts firing. Now only; for the GPU's workloads in the past, metric_history "
         "gpu_workload_sm."),
        (t_list_metrics, "list_metrics", "The named series metric_history and correlate read, with "
         "unit and meaning."),
        (t_metric_history, "metric_history", "One named series over a window, per node (and per "
         "interface for network ones): min, avg, max and when, first and last. With points, the "
         "values too, downsampled to at most 240."),
        (t_alerts, "alerts", "Alert episodes in a window: which alert, how severe, on which node, "
         "when it started, fired and ended."),
        (t_events, "events", "Events in a window: deploys, model loads and cold starts, maintenance "
         "windows, alerts firing."),
        (t_latency, "latency", "Time to first token per model over the last 15 minutes, p50 and p95, "
         "with request rate."),
        (t_clients, "clients", "Who called the engines through the gateway in a window: harness, "
         "machine, model, engine, requests, failures, tokens. Empty when client stats are off."),
        (t_correlate, "correlate", "What moved around a moment: every named series on every node, "
         "ranked by how far it left its baseline, with the alerts and events in the same span. Start "
         "here to isolate a slowdown or a spike."),
        (t_promql, "promql", "A raw PromQL query, instant or over a range. Only when the named "
         "series do not cover the question."),
    ]
    # Each returns compact JSON as one text block.
    for fn, name, desc in tools:
        server.add_tool(fn, name=name, description=desc, annotations=READ_ONLY,
                        structured_output=False)
    return server


#: The SDK's DNS-rebinding guard checks Host against a list; the dashboard is
#: reached by LAN address and by tunnel hostname, and the key below is what
#: stops a page in a browser from using the endpoint, since a page cannot
#: attach it.
TRANSPORT = TransportSecuritySettings(enable_dns_rebinding_protection=False)


class KeyGate:
    """ASGI in front of the MCP transport: off is a 404, a missing or wrong
    key is a 401, and only then the SDK sees the request."""

    def __init__(self, enabled: Callable[[], bool], key: Callable[[], str | None],
                 inner: Callable[[], Any]):
        self._enabled, self._key, self._inner = enabled, key, inner

    async def __call__(self, scope, receive, send):
        if not self._enabled():
            return await _plain(send, 404, "MCP tools are off; switch them on in Settings")
        expected = self._key()
        given = ""
        for name, value in scope.get("headers") or []:
            if name == b"authorization":
                given = value.decode("latin-1")
                break
        token = given[7:].strip() if given[:7].lower() == "bearer " else ""
        if not expected or not secrets.compare_digest(token.encode(), expected.encode()):
            return await _plain(send, 401, "a bearer key is required; Settings makes one",
                                [(b"www-authenticate", b'Bearer realm="spark-dash"')])
        await self._inner().handle_request(scope, receive, send)


async def _plain(send, status: int, text: str, extra: list | None = None):
    body = text.encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode()), *(extra or [])]})
    await send({"type": "http.response.body", "body": body})


# --- the switches and the key -------------------------------------------------

@dataclass(frozen=True)
class McpConfig:
    enabled: bool = False
    promql: bool = False


def parse_mcp(payload: object) -> McpConfig:
    if not isinstance(payload, dict) or payload.get("mcp") is None:
        return McpConfig()
    block = payload["mcp"]
    if not isinstance(block, dict):
        raise ClusterConfigError("`mcp:` must be a mapping with `enabled:` and `promql:`")
    out = {}
    for name in ("enabled", "promql"):
        value = block.get(name, False)
        if not isinstance(value, bool):
            raise ClusterConfigError(f"mcp.{name} must be true or false, not {value!r}")
        out[name] = value
    return McpConfig(**out)


def load_mcp(path: Path) -> McpConfig:
    try:
        payload = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        return McpConfig()
    except (OSError, yaml.YAMLError) as exc:
        raise ClusterConfigError(f"could not read {path}: {exc}") from exc
    return parse_mcp(payload)


def write_mcp(path: Path, config: McpConfig) -> None:
    """Set the block, leaving the rest of the file alone. Off and no PromQL
    removes it."""
    doc = _current_document(path)
    if doc is None or not isinstance(doc.get("nodes"), list):
        raise NoClusterFileError(
            f"MCP tools are configured in the cluster file ({path.name}), and this deployment "
            "does not have one yet. Nodes still come from SPARK_NODES; move them into the "
            "cluster file first."
        )
    nodes = parse_cluster(path.read_text())
    updated = dict(doc)
    if not config.enabled and not config.promql:
        updated.pop("mcp", None)
    else:
        updated["mcp"] = {"enabled": config.enabled, "promql": config.promql}
    text = dump_cluster(nodes, preserve=updated)
    parse_mcp(yaml.safe_load(text))
    _atomic_write(path, text)


def key_path(cluster_path: Path) -> Path:
    return cluster_path.parent / KEY_FILE


def read_key(cluster_path: Path) -> str | None:
    try:
        return key_path(cluster_path).read_text().strip() or None
    except OSError:
        return None


def new_key(cluster_path: Path) -> str:
    """A fresh key, owner-only, written atomically. Returned once, for
    Settings to show; afterwards only its last four are."""
    key = "sdk-" + secrets.token_urlsafe(32)
    p = key_path(cluster_path)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key + "\n")
    os.replace(tmp, p)
    return key


def remove_key(cluster_path: Path) -> None:
    key_path(cluster_path).unlink(missing_ok=True)
