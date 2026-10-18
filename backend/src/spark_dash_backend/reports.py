"""Reports (roadmap BI): scheduled, on demand, kept and downloadable.

A report is a window, a set of nodes and an ordered list of sections, built
from the same reads the agent tools make (agent_tools), into one document.
Every figure is computed, never generated, so a report is correct with the
assistant off; with it on, an optional written summary heads the report and
is marked as the model's.

ONE DOCUMENT, SEVERAL RENDERINGS. A section is text lines and tables of plain
values, so the same document renders to the page, to Markdown, to a
self-contained HTML file (whose print stylesheet is the PDF), to CSV (a file
per table) and to JSON, without a renderer knowing what any section means.
"""

from __future__ import annotations

import asyncio
import calendar
import contextlib
import csv
import html
import io
import json
import math
import os
import re
import secrets
import time
import zipfile
import zoneinfo
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from . import agent_tools as T
from .cluster import (
    ClusterConfigError,
    NoClusterFileError,
    _atomic_write,
    _current_document,
    dump_cluster,
    parse_cluster,
)

# --- sections ----------------------------------------------------------------------

#: id -> (title, what it holds). In the order a report shows them.
SECTIONS: dict[str, tuple[str, str]] = {
    "overview": ("Overview", "Per node: GPU use, temperature, memory, power and throughput, average and peak"),
    "workloads": ("What used the GPU", "Per workload (runtime and model): share of GPU compute and memory held"),
    "models": ("Models", "Loads and how long they took, decode throughput, time to first token"),
    "alerts": ("Alerts", "Every alert episode in the period"),
    "events": ("Events", "Deploys, model loads and cold starts, maintenance, notes"),
    "temperatures": ("Temperatures", "Per node: the hottest sensor in each group, and the least headroom"),
    "network": ("Network", "Per interface: traffic in and out, errors and drops"),
    "clients": ("Clients", "Who called the engines through the gateway"),
    "health": ("Monitoring health", "Gaps in the data, so the report says where its own numbers have holes"),
    "updates": ("DGX OS updates", "Each node's release, pending packages and firmware"),
    "comparison": ("Against the previous period", "What moved most against the period before, of the same length"),
}

DEFAULT_SECTIONS = ["overview", "workloads", "models", "alerts", "temperatures", "health", "comparison"]


def _fmt(v: Any, unit: str = "") -> Any:
    """A plain value for a table cell: rounded, never NaN."""
    if v is None:
        return None
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        v = round(v, 1) if abs(v) >= 10 else round(v, 2)
    return v


def _gib(b: Any) -> Any:
    return None if b is None else round(b / 2**30, 2)


def _bits(b: Any) -> Any:
    """Bits per second as megabits, the unit the Network card uses for most links."""
    return None if b is None else round(b / 1e6, 2)


@dataclass
class Ctx:
    src: T.Sources
    start: float
    end: float
    nodes: list[str] | None
    tz: ZoneInfo = ZoneInfo("UTC")

    def when(self, t: str | None) -> str | None:
        """A moment in the report's own zone, with the date: a day-long
        report spans two dates, and "21:23" alone would not say which."""
        if not t:
            return None
        try:
            d = datetime.fromisoformat(t.replace("Z", "+00:00"))
        except ValueError:
            return t
        return d.astimezone(self.tz).strftime("%m-%d %H:%M")

    @property
    def minutes(self) -> int:
        return max(1, int(round((self.end - self.start) / 60)))

    @property
    def end_iso(self) -> str:
        return T.iso(self.end)

    def keep(self, node: str | None) -> bool:
        return not self.nodes or node in self.nodes


def _table(title: str, columns: list[tuple[str, str]], rows: list[dict]) -> dict:
    return {"title": title, "columns": [{"key": k, "label": label} for k, label in columns], "rows": rows}


async def _history(ctx: Ctx, metric: str) -> list[dict]:
    try:
        r = await T.metric_history(ctx.src, metric, ctx.minutes, ctx.end_iso, ctx.nodes or None)
    except T.ToolError:
        return []
    return r["series"]


async def s_overview(ctx: Ctx) -> dict:
    keys = ["gpu_utilization", "gpu_temperature", "memory_used_percent", "gpu_power", "cpu_utilization",
            "tokens_per_second"]
    got = dict(zip(keys, await asyncio.gather(*(_history(ctx, k) for k in keys)), strict=True))
    nodes = sorted({s["node"] for ss in got.values() for s in ss if s.get("node")})
    rows, lines = [], []
    for n in nodes:
        def summ(k, node=n):
            return next((s["summary"] for s in got[k] if s.get("node") == node and s.get("summary")), None) or {}
        g, t, m, p, c, k = (summ(x) for x in keys)
        rows.append({
            "node": n,
            "gpu_avg": _fmt(g.get("avg")), "gpu_peak": _fmt(g.get("max")), "gpu_peak_at": ctx.when(g.get("max_at")),
            "temp_avg": _fmt(t.get("avg")), "temp_peak": _fmt(t.get("max")), "temp_peak_at": ctx.when(t.get("max_at")),
            "mem_avg": _fmt(m.get("avg")), "mem_peak": _fmt(m.get("max")),
            "power_avg": _fmt(p.get("avg")), "power_peak": _fmt(p.get("max")),
            "cpu_avg": _fmt(c.get("avg")),
            "tok_avg": _fmt(k.get("avg")), "tok_peak": _fmt(k.get("max")),
        })
    if rows:
        busiest = max(rows, key=lambda r: r["gpu_avg"] or 0)
        hottest = max(rows, key=lambda r: r["temp_peak"] or 0)
        lines.append(f"Busiest: {busiest['node']}, GPU {busiest['gpu_avg']}% on average, peaking at "
                     f"{busiest['gpu_peak']}% at {busiest['gpu_peak_at']}.")
        lines.append(f"Hottest GPU: {hottest['node']} at {hottest['temp_peak']} °C ({hottest['temp_peak_at']}).")
    return {"lines": lines, "tables": [_table("Per node", [
        ("node", "Node"), ("gpu_avg", "GPU % avg"), ("gpu_peak", "GPU % peak"), ("gpu_peak_at", "at"),
        ("temp_avg", "GPU °C avg"), ("temp_peak", "GPU °C peak"), ("temp_peak_at", "at"),
        ("mem_avg", "Memory % avg"), ("mem_peak", "Memory % peak"), ("power_avg", "W avg"),
        ("power_peak", "W peak"), ("cpu_avg", "CPU % avg"), ("tok_avg", "tok/s avg"), ("tok_peak", "tok/s peak"),
    ], rows)]}


async def s_workloads(ctx: Ctx) -> dict:
    sm, mem = await asyncio.gather(_history(ctx, "gpu_workload_sm"), _history(ctx, "gpu_workload_memory_bytes"))
    key = lambda s: (s.get("node"), s.get("runtime"), s.get("model"))  # noqa: E731
    memo = {key(s): s.get("summary") or {} for s in mem}
    rows = []
    for s in sm:
        a = s.get("summary") or {}
        m = memo.get(key(s), {})
        if not (a.get("max") or m.get("max")):
            continue
        rows.append({"node": s.get("node"), "runtime": s.get("runtime"), "model": s.get("model"),
                     "sm_avg": _fmt(a.get("avg")), "sm_peak": _fmt(a.get("max")), "sm_peak_at": ctx.when(a.get("max_at")),
                     "mem_peak_gib": _gib(m.get("max"))})
    rows.sort(key=lambda r: -(r["sm_avg"] or 0))
    lines = []
    if rows:
        top = rows[0]
        lines.append(f"Most GPU compute: {top['runtime']}{' ' + top['model'] if top['model'] else ''} on "
                     f"{top['node']}, {top['sm_avg']}% on average.")
    return {"lines": lines, "tables": [_table("Per workload", [
        ("node", "Node"), ("runtime", "Runtime"), ("model", "Model"), ("sm_avg", "SM % avg"),
        ("sm_peak", "SM % peak"), ("sm_peak_at", "at"), ("mem_peak_gib", "Memory GiB peak"),
    ], rows)]}


async def s_models(ctx: Ctx) -> dict:
    try:
        r = await T.model_activity(ctx.src, None, ctx.minutes, ctx.end_iso)
    except T.ToolError:
        return {"lines": ["Model history was not available."], "tables": []}
    rows = []
    for m in r["models"]:
        loads = [x for x in m.get("loads", []) if ctx.keep(x.get("node"))]
        dec = [x for x in m.get("decode_tok_s", []) if ctx.keep(x.get("node"))]
        ttft = m.get("time_to_first_token") or {}
        rows.append({
            "model": m["model"], "loads": len(loads),
            "load_s": _fmt(sum(x["seconds"] for x in loads) / len(loads)) if loads else None,
            "decode_avg": _fmt(max((x["avg"] or 0) for x in dec)) if dec else None,
            "decode_peak": _fmt(max((x["max"] or 0) for x in dec)) if dec else None,
            "requests": ttft.get("requests"),
            "ttft_p50": _fmt((ttft.get("p50") or {}).get("avg_s")),
            "ttft_p95_peak": _fmt((ttft.get("p95") or {}).get("max_s")),
        })
    rows.sort(key=lambda x: -((x["requests"] or 0) + (x["loads"] or 0)))
    return {"lines": [], "tables": [_table("Per model", [
        ("model", "Model"), ("loads", "Loads"), ("load_s", "Load s avg"), ("decode_avg", "Decode tok/s avg"),
        ("decode_peak", "Decode tok/s peak"), ("requests", "Requests"), ("ttft_p50", "First token p50 s"),
        ("ttft_p95_peak", "First token p95 s peak"),
    ], rows)]}


async def s_alerts(ctx: Ctx) -> dict:
    try:
        r = await T.alerts(ctx.src, ctx.minutes, ctx.end_iso)
    except T.ToolError:
        return {"lines": ["Alert history was not available."], "tables": []}
    eps = [e for e in r["episodes"] if ctx.keep(e.get("node")) or e.get("node") is None]
    fired = [e for e in eps if e.get("fired")]
    lines = [f"{len(fired)} alert{'s' if len(fired) != 1 else ''} fired"
             + (f", {len(eps) - len(fired)} more pending only." if len(eps) > len(fired) else ".")]
    rows = [{"alert": e["alert"], "severity": e.get("severity"), "node": e.get("node"),
             "started": ctx.when(e.get("started")), "fired": ctx.when(e.get("fired")),
             "ended": "ongoing" if e.get("ongoing") else ctx.when(e.get("ended"))} for e in eps]
    return {"lines": lines, "tables": [_table("Alert episodes", [
        ("alert", "Alert"), ("severity", "Severity"), ("node", "Node"), ("started", "Started"),
        ("fired", "Fired"), ("ended", "Ended"),
    ], rows)]}


async def s_events(ctx: Ctx) -> dict:
    r = await T.events(ctx.src, ctx.minutes, ctx.end_iso)
    rows = [{"at": ctx.when(e["at"]), "kind": e["kind"], "what": e["what"],
             "node": e.get("node")} for e in r["events"] if ctx.keep(e.get("node")) or not e.get("node")]
    return {"lines": [], "tables": [_table("Events", [("at", "At"), ("kind", "Kind"), ("what", "What"),
                                                      ("node", "Node")], rows)]}


async def s_temperatures(ctx: Ctx) -> dict:
    keys = ["system_temperature", "package_temperature", "gpu_temperature", "storage_temperature",
            "network_temperature", "temperature_headroom"]
    got = dict(zip(keys, await asyncio.gather(*(_history(ctx, k) for k in keys)), strict=True))
    nodes = sorted({s["node"] for ss in got.values() for s in ss if s.get("node")})
    rows = []
    for n in nodes:
        def s(k, node=n):
            return next((x["summary"] for x in got[k] if x.get("node") == node and x.get("summary")), None) or {}
        h = s("temperature_headroom")
        rows.append({"node": n, "hottest": _fmt(s("system_temperature").get("max")),
                     "package": _fmt(s("package_temperature").get("max")), "gpu": _fmt(s("gpu_temperature").get("max")),
                     "storage": _fmt(s("storage_temperature").get("max")),
                     "network": _fmt(s("network_temperature").get("max")),
                     "headroom_min": _fmt(h.get("min")), "headroom_at": ctx.when(h.get("min_at"))})
    lines = []
    if rows and any(r["headroom_min"] is not None for r in rows):
        tight = min((r for r in rows if r["headroom_min"] is not None), key=lambda r: r["headroom_min"])
        lines.append(f"Closest to a limit: {tight['node']}, {tight['headroom_min']} °C of headroom at "
                     f"{tight['headroom_at']}.")
    return {"lines": lines, "tables": [_table("Peak per node (°C)", [
        ("node", "Node"), ("hottest", "Hottest sensor"), ("package", "Processor"), ("gpu", "GPU"),
        ("storage", "Storage"), ("network", "Network"), ("headroom_min", "Least headroom"), ("headroom_at", "at"),
    ], rows)]}


async def s_network(ctx: Ctx) -> dict:
    keys = ["network_rx_bits", "network_tx_bits", "network_errors", "network_drops"]
    got = dict(zip(keys, await asyncio.gather(*(_history(ctx, k) for k in keys)), strict=True))
    by: dict[tuple, dict] = {}
    for k, ss in got.items():
        for s in ss:
            by.setdefault((s.get("node"), s.get("interface")), {})[k] = s.get("summary") or {}
    rows = []
    for (node, iface), v in sorted(by.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "")):
        rx, tx = v.get("network_rx_bits", {}), v.get("network_tx_bits", {})
        if not (rx.get("max") or tx.get("max") or v.get("network_errors", {}).get("max")
                or v.get("network_drops", {}).get("max")):
            continue
        rows.append({"node": node, "interface": iface, "rx_avg": _bits(rx.get("avg")), "rx_peak": _bits(rx.get("max")),
                     "tx_avg": _bits(tx.get("avg")), "tx_peak": _bits(tx.get("max")),
                     "errors_peak": _fmt(v.get("network_errors", {}).get("max")),
                     "drops_peak": _fmt(v.get("network_drops", {}).get("max"))})
    return {"lines": [], "tables": [_table("Per interface (Mb/s)", [
        ("node", "Node"), ("interface", "Interface"), ("rx_avg", "In avg"), ("rx_peak", "In peak"),
        ("tx_avg", "Out avg"), ("tx_peak", "Out peak"), ("errors_peak", "Errors, worst step"),
        ("drops_peak", "Drops, worst step"),
    ], rows)]}


async def s_clients(ctx: Ctx) -> dict:
    r = await T.clients(ctx.src, max(5, ctx.minutes))
    if not r.get("configured"):
        return {"lines": ["Client stats are off: there is no gateway in front of the engines."], "tables": []}
    rows = [{"harness": (x.get("harness") or {}).get("name"), "client": (x.get("client") or {}).get("name"),
             "model": x.get("model"), "requests": x.get("requests"), "failed": x.get("failed"),
             "tokens_in": x.get("tokens_in"), "tokens_out": x.get("tokens_out")} for x in r.get("rows", [])]
    note = ["The gateway's figures run up to now, not to the report's end."] if time.time() - ctx.end > 300 else []
    return {"lines": note, "tables": [_table("Callers", [
        ("harness", "Harness"), ("client", "Client"), ("model", "Model"), ("requests", "Requests"),
        ("failed", "Failed"), ("tokens_in", "Tokens in"), ("tokens_out", "Tokens out"),
    ], rows)]}


async def s_health(ctx: Ctx) -> dict:
    r = await T.monitoring_health(ctx.src, ctx.minutes, ctx.end_iso)
    rows = []
    for t in r["targets_with_gaps_or_down"]:
        for g in t["gaps"] or [[None, None]]:
            rows.append({"job": t.get("job"), "node": t.get("node"), "instance": t.get("instance"),
                         "from": ctx.when(g[0]), "to": ctx.when(g[1])})
    lines = [f"{r['targets_healthy']} scrape targets complete for the whole period."
             if not rows else f"{len(rows)} gap{'s' if len(rows) != 1 else ''} in the data: the figures above "
             "are missing, not zero, inside them."]
    return {"lines": lines, "tables": [_table("Gaps", [("job", "Job"), ("node", "Node"), ("instance", "Target"),
                                                       ("from", "From"), ("to", "To")], rows)] if rows else []}


async def s_updates(ctx: Ctx) -> dict:
    r = await T.updates(ctx.src)
    if not r.get("configured"):
        return {"lines": ["DGX OS updates are not set up."], "tables": []}
    rows = [{"node": n.get("name"), "release": (n.get("release") or {}).get("on_name"),
             "latest": (n.get("release") or {}).get("latest_name"), "packages": (n.get("software") or {}).get("behind"),
             "firmware": (n.get("firmware") or {}).get("state")} for n in r["nodes"] if ctx.keep(n.get("name"))]
    return {"lines": [], "tables": [_table("Per node", [("node", "Node"), ("release", "Release"),
                                                        ("latest", "Latest"), ("packages", "Packages behind"),
                                                        ("firmware", "Firmware")], rows)]}


async def s_comparison(ctx: Ctx) -> dict:
    length = ctx.end - ctx.start
    if length > 7 * 86400:
        return {"lines": ["Compared only for periods of a week or less."], "tables": []}
    r = await T.compare(ctx.src, T.iso(ctx.start), ctx.end_iso, max(5, ctx.minutes),
                        ctx.nodes[0] if ctx.nodes and len(ctx.nodes) == 1 else None)
    rows = [{"node": m.get("node"), "what": " ".join(x for x in (m.get("runtime"), m.get("model"), m.get("interface"))
                                                     if x) or None,
             "metric": m["metric"], "unit": m.get("unit"), "before": _fmt(m["a_avg"]), "now": _fmt(m["b_avg"]),
             "change": _fmt(m["change"]), "score": m["score"]}
            for m in r["moved"] if ctx.keep(m.get("node"))][:15]
    lines = [f"Against {ctx.when(r['a'][0])} to {ctx.when(r['a'][1])}, the period before." if rows
             else "Nothing moved notably against the previous period."]
    return {"lines": lines, "tables": [_table("Moved most", [
        ("node", "Node"), ("what", "Which"), ("metric", "Series"), ("unit", "Unit"), ("before", "Before avg"),
        ("now", "This period avg"), ("change", "Change"), ("score", "Score"),
    ], rows)] if rows else []}


BUILDERS: dict[str, Callable[[Ctx], Awaitable[dict]]] = {
    "overview": s_overview, "workloads": s_workloads, "models": s_models, "alerts": s_alerts,
    "events": s_events, "temperatures": s_temperatures, "network": s_network, "clients": s_clients,
    "health": s_health, "updates": s_updates, "comparison": s_comparison,
}


# --- building -------------------------------------------------------------------------

#: The longest period a report covers: the tools read at most 31 days.
MAX_SPAN_S = 31 * 86400


def headline(doc: dict) -> list[str]:
    """The few figures a notification carries."""
    out = []
    for sec in doc["sections"]:
        if sec["id"] in ("overview", "workloads", "alerts", "temperatures") and sec.get("lines"):
            out.append(sec["lines"][0])
    return out[:4]


async def build(src: T.Sources, *, start: float, end: float, sections: list[str], nodes: list[str] | None = None,
                title: str | None = None, tz: str = "UTC", schedule: dict | None = None,
                narrate: Callable[[str], Awaitable[dict | None]] | None = None) -> dict:
    if end <= start:
        raise ValueError("a report's end must come after its start")
    if end - start > MAX_SPAN_S:
        raise ValueError("a report covers at most 31 days")
    unknown = [s for s in sections if s not in BUILDERS]
    if unknown:
        raise ValueError(f"unknown sections: {unknown}; known: {list(BUILDERS)}")
    if not sections:
        raise ValueError("choose at least one section")
    ctx = Ctx(src, start, end, nodes or None, ZoneInfo(tz))
    t0 = time.monotonic()
    # In the order given; built together, since each is a handful of queries.
    results = await asyncio.gather(*(BUILDERS[s](ctx) for s in sections), return_exceptions=True)
    out = []
    for sid, r in zip(sections, results, strict=True):
        name, about = SECTIONS[sid]
        if isinstance(r, BaseException):
            r = {"lines": [f"This section could not be built: {r}"], "tables": [], "error": True}
        out.append({"id": sid, "title": name, "about": about, **r})
    zone = ZoneInfo(tz)
    local = lambda t: datetime.fromtimestamp(t, zone).strftime("%Y-%m-%d %H:%M")  # noqa: E731
    doc = {
        "id": datetime.fromtimestamp(time.time(), UTC).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3),
        "title": title or f"Cluster report, {local(start)} to {local(end)} ({tz})",
        "created": T.iso(time.time()),
        "window": {"start": T.iso(start), "end": T.iso(end), "tz": tz,
                   "start_local": local(start), "end_local": local(end)},
        "nodes": nodes or None,
        "schedule": schedule,
        "sections": out,
        "narrative": None,
        "seconds": round(time.monotonic() - t0, 2),
    }
    doc["headline"] = headline(doc)
    if narrate:
        try:
            doc["narrative"] = await narrate(render_markdown(doc))
        except Exception as exc:  # noqa: BLE001 — the report stands without it
            doc["narrative"] = {"error": f"the assistant did not answer: {exc}"}
    return doc


# --- rendering --------------------------------------------------------------------------

def _cell(v: Any) -> str:
    return "—" if v is None or v == "" else str(v)


def render_markdown(doc: dict) -> str:
    w = doc["window"]
    lines = [f"# {doc['title']}", "",
             f"{w['start_local']} to {w['end_local']}, times in {w['tz']} (UTC {w['start']} to {w['end']}).",
             f"Nodes: {', '.join(doc['nodes']) if doc.get('nodes') else 'all'}. Generated {doc['created']}.", ""]
    n = doc.get("narrative") or {}
    if n.get("text"):
        lines += [f"> **Summary, written by {n.get('model', 'the assistant')}:** {n['text']}", ""]
    for sec in doc["sections"]:
        lines += [f"## {sec['title']}", ""]
        lines += [*(f"- {x}" for x in sec.get("lines", [])), ""] if sec.get("lines") else []
        for t in sec.get("tables", []):
            if not t["rows"]:
                lines += [f"*{t['title']}: nothing in this period.*", ""]
                continue
            cols = t["columns"]
            lines += [f"**{t['title']}**", "",
                      "| " + " | ".join(c["label"] for c in cols) + " |",
                      "|" + "|".join("---" for _ in cols) + "|"]
            lines += ["| " + " | ".join(_cell(r.get(c["key"])).replace("|", "\\|") for c in cols) + " |"
                      for r in t["rows"]]
            lines.append("")
    return "\n".join(lines)


_CSS = """
:root{--ink:#111;--muted:#555;--rule:#d9d9d4;--band:#f4f4f0;--accent:#2873cf}
*{box-sizing:border-box}body{margin:0;background:#fff;color:var(--ink);
font:13px/1.5 ui-monospace,"SF Mono","JetBrains Mono",Menlo,monospace;padding:28px max(16px,4vw)}
h1{font-size:20px;margin:0 0 4px}h2{font-size:14px;letter-spacing:.06em;text-transform:uppercase;
margin:28px 0 8px;padding-bottom:4px;border-bottom:2px solid var(--ink)}
.meta{color:var(--muted);margin:0 0 16px}.summary{border-left:3px solid var(--accent);padding:8px 12px;
background:var(--band);margin:12px 0}.about{color:var(--muted);margin:0 0 8px}
ul{margin:0 0 10px;padding-left:18px}table{border-collapse:collapse;width:100%;margin:6px 0 14px;
font-variant-numeric:tabular-nums}caption{text-align:left;font-weight:600;padding:4px 0}
th,td{border-bottom:1px solid var(--rule);padding:4px 8px;text-align:left;vertical-align:top}
th{background:var(--band);font-weight:600}td.n{text-align:right}.none{color:var(--muted);font-style:italic}
.wrap{overflow-x:auto}
@media print{body{padding:0}h2{break-after:avoid}table{break-inside:auto}tr{break-inside:avoid}
.wrap{overflow:visible}@page{margin:14mm}}
"""


def render_html(doc: dict, *, auto_print: bool = False) -> str:
    """Self-contained: one file, no fetches, so it opens anywhere and prints
    as the PDF."""
    e = html.escape
    w = doc["window"]
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
             f"content='width=device-width,initial-scale=1'><title>{e(doc['title'])}</title><style>{_CSS}</style>"
             "</head><body>", f"<h1>{e(doc['title'])}</h1>",
             f"<p class='meta'>{e(w['start_local'])} to {e(w['end_local'])} ({e(w['tz'])}) · "
             f"nodes: {e(', '.join(doc['nodes']) if doc.get('nodes') else 'all')} · generated {e(doc['created'])}</p>"]
    n = doc.get("narrative") or {}
    if n.get("text"):
        parts.append(f"<div class='summary'><b>Summary, written by {e(n.get('model', 'the assistant'))}:</b> "
                     f"{e(n['text'])}</div>")
    for sec in doc["sections"]:
        parts.append(f"<h2>{e(sec['title'])}</h2><p class='about'>{e(sec.get('about', ''))}</p>")
        if sec.get("lines"):
            parts.append("<ul>" + "".join(f"<li>{e(x)}</li>" for x in sec["lines"]) + "</ul>")
        for t in sec.get("tables", []):
            if not t["rows"]:
                parts.append(f"<p class='none'>{e(t['title'])}: nothing in this period.</p>")
                continue
            head = "".join(f"<th>{e(c['label'])}</th>" for c in t["columns"])
            body = "".join(
                "<tr>" + "".join(
                    f"<td class='{'n' if isinstance(r.get(c['key']), (int, float)) else ''}'>"
                    f"{e(_cell(r.get(c['key'])))}</td>" for c in t["columns"]) + "</tr>"
                for r in t["rows"])
            parts.append(f"<div class='wrap'><table><caption>{e(t['title'])}</caption><thead><tr>{head}</tr></thead>"
                         f"<tbody>{body}</tbody></table></div>")
    if auto_print:
        parts.append("<script>addEventListener('load',()=>setTimeout(()=>print(),200))</script>")
    parts.append("</body></html>")
    return "".join(parts)


#: The PDF's built-in fonts cover Latin-1; the few characters a report uses
#: outside it are mapped to plain ones rather than a font being shipped.
_PDF_PLAIN = str.maketrans({"—": "-", "–": "-", "’": "'", "‘": "'", "“": '"', "”": '"', "…": "...",
                            "→": "->", "•": "-", "≥": ">=", "≤": "<=", "\u00a0": " "})


def _pdf_text(v: Any) -> str:
    return _cell(v).translate(_PDF_PLAIN).encode("latin-1", "replace").decode("latin-1")


def _col_widths(t: dict) -> tuple[float, ...]:
    """Each column's share of the table width, from the longest text it
    holds, within bounds: equal columns wrapped a model's name while number
    columns sat half empty."""
    lens = []
    for c in t["columns"]:
        longest = max([len(_cell(c["label"]))] + [len(_cell(r.get(c["key"]))) for r in t["rows"]])
        lens.append(min(max(longest, 4), 34))
    return tuple(float(x) for x in lens)


ORIENTATIONS = ("landscape", "portrait")


def render_pdf(doc: dict, orientation: str = "landscape") -> bytes:
    """A4, landscape by default so the per-node tables fit, or portrait,
    made here without a print dialog: the browser lays a printed page out
    for the default printer's paper, which on a Mac whose only printer is
    an Instax made print-to-PDF unusable (2026-10-17)."""
    from fpdf import FPDF
    from fpdf.fonts import FontFace

    if orientation not in ORIENTATIONS:
        raise ValueError(f"orientation is one of {ORIENTATIONS}")
    portrait = orientation == "portrait"
    pdf = FPDF(orientation="P" if portrait else "L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.set_margins(12, 12, 12)
    pdf.set_title(_pdf_text(doc["title"]))
    pdf.set_creator("spark-dash")
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin
    w = doc["window"]

    pdf.set_font("Helvetica", "B", 15)
    pdf.multi_cell(width, 7, _pdf_text(doc["title"]), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(90, 90, 90)
    pdf.multi_cell(width, 4.5, _pdf_text(
        f"{w['start_local']} to {w['end_local']}, times in {w['tz']}  ·  nodes: "
        f"{', '.join(doc['nodes']) if doc.get('nodes') else 'all'}  ·  generated {doc['created']}"),
        new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    n = doc.get("narrative") or {}
    if n.get("text"):
        pdf.ln(2)
        pdf.set_fill_color(240, 240, 236)
        pdf.set_font("Helvetica", "B", 9)
        pdf.multi_cell(width, 5, _pdf_text(f"Summary, written by {n.get('model', 'the assistant')}:"),
                       fill=True, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(width, 4.8, _pdf_text(n["text"]), fill=True, new_x="LMARGIN", new_y="NEXT")

    head = FontFace(emphasis="BOLD", fill_color=(236, 236, 230))
    for sec in doc["sections"]:
        # A heading with no room for anything under it starts the next page
        # instead of sitting alone at the foot of this one.
        if pdf.get_y() > pdf.h - pdf.b_margin - 35:
            pdf.add_page()
        else:
            pdf.ln(4)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(width, 6, _pdf_text(sec["title"].upper()), new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(0, 0, 0)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + width, pdf.get_y())
        pdf.ln(1)
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(90, 90, 90)
        pdf.multi_cell(width, 4, _pdf_text(sec.get("about", "")), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "", 9)
        for line in sec.get("lines", []):
            pdf.multi_cell(width, 4.6, _pdf_text(f"- {line}"), new_x="LMARGIN", new_y="NEXT")
        for t in sec.get("tables", []):
            pdf.ln(1.5)
            pdf.set_font("Helvetica", "B", 8.5)
            pdf.cell(width, 5, _pdf_text(t["title"]), new_x="LMARGIN", new_y="NEXT")
            if not t["rows"]:
                pdf.set_font("Helvetica", "I", 8)
                pdf.cell(width, 4.5, "Nothing in this period.", new_x="LMARGIN", new_y="NEXT")
                continue
            # Smaller type on a narrower page and for wider tables.
            cols = len(t["columns"])
            size = 7.5 if cols <= 8 else 6.5
            if portrait:
                size = 7 if cols <= 6 else 6 if cols <= 9 else 5.2
            pdf.set_font("Helvetica", "", size)
            with pdf.table(headings_style=head, line_height=size * 0.55, padding=0.8, width=width,
                           col_widths=_col_widths(t), first_row_as_headings=True) as table:
                row = table.row()
                for c in t["columns"]:
                    row.cell(_pdf_text(c["label"]))
                for r in t["rows"]:
                    row = table.row()
                    for c in t["columns"]:
                        v = r.get(c["key"])
                        row.cell(_pdf_text(v), align="R" if isinstance(v, (int, float)) else "L")
    return bytes(pdf.output())


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in s.lower()).strip("-")[:40] or "table"


def render_csv_zip(doc: dict) -> bytes:
    """One CSV per table, named section-table.csv."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, sec in enumerate(doc["sections"], 1):
            for t in sec.get("tables", []):
                out = io.StringIO()
                wr = csv.writer(out)
                wr.writerow([c["label"] for c in t["columns"]])
                for r in t["rows"]:
                    wr.writerow(["" if r.get(c["key"]) is None else r.get(c["key"]) for c in t["columns"]])
                z.writestr(f"{i:02d}-{sec['id']}-{_slug(t['title'])}.csv", out.getvalue())
        z.writestr("report.json", json.dumps(doc, ensure_ascii=False, indent=1))
    return buf.getvalue()


# --- kept reports ------------------------------------------------------------------------

#: A report's file name: its id, `YYYYMMDD-HHMMSS-hex6`. Only these are
#: reports; other files share the directory (the schedules' state), and a
#: glob for `*.json` read that one as a report and failed the whole list
#: (2026-10-17), and would have let pruning delete it.
_REPORT_FILE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}\.json$")


class ReportStore:
    """Reports as JSON files in the cluster directory's `reports/`; the
    renderings are made from them on request."""

    def __init__(self, cluster_path: Path):
        self.dir = cluster_path.parent / "reports"

    def _files(self) -> list[Path]:
        """Report files only, newest first."""
        if not self.dir.is_dir():
            return []
        return sorted((p for p in self.dir.iterdir() if _REPORT_FILE.match(p.name)), reverse=True)

    def _path(self, rid: str) -> Path:
        if not rid or any(c not in "0123456789abcdef-" for c in rid):
            raise KeyError(rid)
        return self.dir / f"{rid}.json"

    def save(self, doc: dict) -> None:
        # Only an id `list()` will find: a report kept under any other name
        # would be saved and never shown.
        if not _REPORT_FILE.match(f"{doc.get('id')}.json"):
            raise KeyError(doc.get("id"))
        self.dir.mkdir(exist_ok=True)
        tmp = self.dir / f".{doc['id']}.tmp"
        tmp.write_text(json.dumps(doc, ensure_ascii=False))
        os.replace(tmp, self._path(doc["id"]))

    def get(self, rid: str) -> dict:
        try:
            return json.loads(self._path(rid).read_text())
        except (OSError, ValueError) as exc:
            raise KeyError(rid) from exc

    def delete(self, rid: str) -> None:
        self._path(rid).unlink(missing_ok=True)

    def list(self) -> list[dict]:
        out = []
        for p in self._files():
            try:
                d = json.loads(p.read_text())
                d["id"], d["title"], d["created"], d["window"], d["sections"]  # noqa: B018 — a report, or skipped
            except (OSError, ValueError, KeyError, TypeError):
                continue
            out.append({"id": d["id"], "title": d["title"], "created": d["created"], "window": d["window"],
                        "schedule": (d.get("schedule") or {}).get("name"), "headline": d.get("headline", []),
                        "sections": [s["id"] for s in d["sections"]], "narrative": bool((d.get("narrative") or {}).get("text"))})
        return out

    def prune(self, keep_count: int, keep_days: int) -> int:
        """Oldest first past `keep_count`, and any older than `keep_days`."""
        files = self._files()
        cutoff = time.time() - keep_days * 86400
        gone = 0
        for i, p in enumerate(files):
            if i >= keep_count or p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                gone += 1
        return gone


# --- time zone ----------------------------------------------------------------------------

_HOST_TZ: str | None = None


def _zone_named_by_content(localtime: Path) -> str | None:
    """The zone whose data file matches the host's /etc/localtime, which is
    what a bind mount of that symlink delivers: the file, not its name.
    Several names share data (Europe/Moscow and W-SU); a Region/City name
    wins over an alias."""
    try:
        data = localtime.read_bytes()
    except OSError:
        return None
    roots = [Path(p) for p in zoneinfo.TZPATH] or [Path("/usr/share/zoneinfo")]
    matches = []
    for name in zoneinfo.available_timezones():
        for root in roots:
            f = root / name
            with contextlib.suppress(OSError):
                if f.stat().st_size == len(data) and f.read_bytes() == data:
                    matches.append(name)
                break
    if not matches:
        return None
    regional = sorted(m for m in matches if "/" in m and not m.startswith(("Etc/", "SystemV/", "US/")))
    return (regional or sorted(matches))[0]


def host_timezone(host_file: Path = Path("/host/etc/timezone"),
                  host_localtime: Path = Path("/host/etc/localtime")) -> str:
    """The monitoring host's zone. A container does not see it unless told,
    so in order: TZ if set; the host's /etc/timezone (Debian, Ubuntu) if
    mounted; the zone whose data matches the host's /etc/localtime, which
    every Linux host has and the compose file mounts read-only; the
    container's own; UTC. Worked out once: the host does not change zone
    under a running backend often enough to re-read it."""
    global _HOST_TZ
    if _HOST_TZ and host_file == Path("/host/etc/timezone"):
        return _HOST_TZ
    candidates = [os.environ.get("TZ", "").strip()]
    with contextlib.suppress(OSError):
        candidates.append(host_file.read_text().strip())
    candidates.append(_zone_named_by_content(host_localtime) or "")
    with contextlib.suppress(OSError):
        candidates.append(Path("/etc/timezone").read_text().strip())
    found = "UTC"
    for c in candidates:
        if c and valid_timezone(c):
            found = c
            break
    if host_file == Path("/host/etc/timezone"):
        _HOST_TZ = found
    return found


def valid_timezone(tz: str) -> bool:
    try:
        ZoneInfo(tz)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


# --- configuration: schedules, retention, time zone ---------------------------------------

FREQUENCIES = ("daily", "weekly", "monthly", "hours")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class Schedule:
    id: str
    name: str
    frequency: str  # daily | weekly | monthly | hours
    time: str = "08:00"  # HH:MM in the configured zone
    weekday: str = "mon"
    day: int = 1  # monthly; past a short month's end, its last day
    every_hours: int = 6
    nodes: tuple[str, ...] = ()
    sections: tuple[str, ...] = tuple(DEFAULT_SECTIONS)
    notify: bool = True
    narrative: bool = False
    enabled: bool = True

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "frequency": self.frequency, "time": self.time,
                "weekday": self.weekday, "day": self.day, "every_hours": self.every_hours,
                "nodes": list(self.nodes), "sections": list(self.sections), "notify": self.notify,
                "narrative": self.narrative, "enabled": self.enabled}


@dataclass(frozen=True)
class ReportsConfig:
    timezone: str | None = None  # None: the host's
    keep_count: int = 200
    keep_days: int = 180
    #: The dashboard's address as a reader reaches it, for links in
    #: notifications: http://monitor:8080 or the tunnel's hostname.
    public_url: str | None = None
    schedules: tuple[Schedule, ...] = ()

    def as_dict(self) -> dict:
        return {"timezone": self.timezone, "keep_count": self.keep_count, "keep_days": self.keep_days,
                "public_url": self.public_url, "schedules": [s.as_dict() for s in self.schedules]}


def parse_schedule(raw: object) -> Schedule:
    if not isinstance(raw, dict):
        raise ClusterConfigError("a report schedule must be a mapping")
    name = str(raw.get("name") or "").strip() or "Report"
    freq = raw.get("frequency")
    if freq not in FREQUENCIES:
        raise ClusterConfigError(f"schedule {name!r}: frequency must be one of {FREQUENCIES}")
    t = raw.get("time", "08:00")
    # YAML 1.1 reads an unquoted 10:30 as the base-60 integer 630; a hand
    # edit of the cluster file must not break the schedule. (Written here it
    # is always quoted.)
    if isinstance(t, int) and not isinstance(t, bool):
        t = f"{t // 60:02d}:{t % 60:02d}"
    t = str(t)
    try:
        hh, mm = (int(x) for x in t.split(":"))
        assert 0 <= hh < 24 and 0 <= mm < 60
    except (ValueError, AssertionError) as exc:
        raise ClusterConfigError(f"schedule {name!r}: time must be HH:MM") from exc
    weekday = str(raw.get("weekday", "mon")).lower()[:3]
    if weekday not in WEEKDAYS:
        raise ClusterConfigError(f"schedule {name!r}: weekday must be one of {WEEKDAYS}")
    day, every = int(raw.get("day", 1)), int(raw.get("every_hours", 6))
    if not 1 <= day <= 31:
        raise ClusterConfigError(f"schedule {name!r}: day must be 1 to 31")
    if not 1 <= every <= 168:
        raise ClusterConfigError(f"schedule {name!r}: every_hours must be 1 to 168")
    sections = tuple(raw.get("sections") or DEFAULT_SECTIONS)
    bad = [s for s in sections if s not in SECTIONS]
    if bad:
        raise ClusterConfigError(f"schedule {name!r}: unknown sections {bad}")
    sid = str(raw.get("id") or "").strip() or secrets.token_hex(4)
    return Schedule(id=sid, name=name, frequency=freq, time=f"{hh:02d}:{mm:02d}", weekday=weekday, day=day,
                    every_hours=every, nodes=tuple(str(n) for n in raw.get("nodes") or ()), sections=sections,
                    notify=bool(raw.get("notify", True)), narrative=bool(raw.get("narrative", False)),
                    enabled=bool(raw.get("enabled", True)))


def parse_reports(payload: object) -> ReportsConfig:
    if not isinstance(payload, dict) or payload.get("reports") is None:
        return ReportsConfig()
    b = payload["reports"]
    if not isinstance(b, dict):
        raise ClusterConfigError("`reports:` must be a mapping")
    tz = b.get("timezone")
    if tz is not None and not valid_timezone(str(tz)):
        raise ClusterConfigError(f"reports.timezone {tz!r} is not a time zone")
    url = b.get("public_url")
    if url is not None:
        url = str(url).strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ClusterConfigError("reports.public_url must start with http:// or https://")
    schedules = tuple(parse_schedule(s) for s in b.get("schedules") or [])
    ids = [s.id for s in schedules]
    if len(set(ids)) != len(ids):
        raise ClusterConfigError("report schedules must have different ids")
    return ReportsConfig(timezone=str(tz) if tz else None, keep_count=max(1, int(b.get("keep_count", 200))),
                         keep_days=max(1, int(b.get("keep_days", 180))), public_url=url or None,
                         schedules=schedules)


def load_reports(path: Path) -> ReportsConfig:
    try:
        payload = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        return ReportsConfig()
    except (OSError, yaml.YAMLError) as exc:
        raise ClusterConfigError(f"could not read {path}: {exc}") from exc
    return parse_reports(payload)


def write_reports(path: Path, config: ReportsConfig) -> None:
    doc = _current_document(path)
    if doc is None or not isinstance(doc.get("nodes"), list):
        raise NoClusterFileError(
            f"reports are configured in the cluster file ({path.name}), and this deployment does not have "
            "one yet. Nodes still come from SPARK_NODES; move them into the cluster file first."
        )
    nodes = parse_cluster(path.read_text())
    updated = dict(doc)
    block = config.as_dict()
    if block["timezone"] is None:
        block.pop("timezone")
    if block["public_url"] is None:
        block.pop("public_url")
    updated["reports"] = block
    text = dump_cluster(nodes, preserve=updated)
    parse_reports(yaml.safe_load(text))
    _atomic_write(path, text)


# --- when a schedule is due ----------------------------------------------------------------

def _at(d: datetime, hhmm: str) -> datetime:
    hh, mm = (int(x) for x in hhmm.split(":"))
    return d.replace(hour=hh, minute=mm, second=0, microsecond=0)


def next_due(s: Schedule, after: datetime, tz: ZoneInfo) -> datetime:
    """The first moment strictly after `after` the schedule fires, as an
    aware datetime in `tz`. Built on local wall-clock time, so "daily at
    08:00" stays 08:00 across a change to or from summer time."""
    a = after.astimezone(tz)
    if s.frequency == "hours":
        # On whole hours, counted from midnight, so "every 6 hours" is 00, 06,
        # 12 and 18 rather than drifting from whenever it was saved.
        base = a.replace(minute=0, second=0, microsecond=0)
        while True:
            base += timedelta(hours=1)
            if base.hour % s.every_hours == 0:
                return base
    if s.frequency == "daily":
        c = _at(a, s.time)
        return c if c > a else _at(a + timedelta(days=1), s.time)
    if s.frequency == "weekly":
        target = WEEKDAYS.index(s.weekday)
        for i in range(8):
            c = _at(a + timedelta(days=i), s.time)
            if c.weekday() == target and c > a:
                return c
    # monthly
    y, m = a.year, a.month
    for _ in range(3):
        last = calendar.monthrange(y, m)[1]
        c = _at(a.replace(year=y, month=m, day=min(s.day, last)), s.time)
        if c > a:
            return c
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    raise AssertionError("unreachable")


def period_start(s: Schedule, due: datetime) -> datetime:
    """What a run at `due` covers: the period since the previous run time."""
    if s.frequency == "hours":
        return due - timedelta(hours=s.every_hours)
    if s.frequency == "daily":
        return due - timedelta(days=1)
    if s.frequency == "weekly":
        return due - timedelta(days=7)
    y, m = (due.year - 1, 12) if due.month == 1 else (due.year, due.month - 1)
    last = calendar.monthrange(y, m)[1]
    return due.replace(year=y, month=m, day=min(s.day, last))


class ScheduleState:
    """When each schedule last ran, beside the reports: a missed run is run
    once on start rather than forgotten or repeated."""

    def __init__(self, store: ReportStore):
        self.path = store.dir / "schedule-state.json"

    def read(self) -> dict[str, dict]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def write(self, state: dict) -> None:
        self.path.parent.mkdir(exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, self.path)


def due_runs(config: ReportsConfig, state: dict, now: datetime, tz: ZoneInfo) -> list[tuple[Schedule, datetime]]:
    """Schedules due now, each with the due time to run for. A schedule seen
    for the first time starts counting from now, so saving one never fires a
    report at once; one that missed several runs runs once, for the latest."""
    out = []
    for s in config.schedules:
        if not s.enabled:
            continue
        seen = state.get(s.id, {})
        anchor = seen.get("last_due") or seen.get("since")
        if not anchor:
            continue
        due = next_due(s, datetime.fromisoformat(anchor), tz)
        if due > now:
            continue
        latest = due
        while (nxt := next_due(s, latest, tz)) <= now:
            latest = nxt
        out.append((s, latest))
    return out
