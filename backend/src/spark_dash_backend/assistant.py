"""Ask the dashboard (roadmap AY).

A question, "why was sparky slow at 9pm?", answered by a model of the
reader's choosing from what the dashboard already has: the alerts and events
in the window, the series that moved, the cluster as it stands. Off until it
is configured.

WHAT THE MODEL CAN DO: read. It is sent text and returns text. With tools
on (roadmap BB4) it may also call the dashboard's read-only tools, the same
ones `/mcp` serves, to look further than the bundle it is sent. Nothing here
acts on what it says, so it can be told about the cluster and never act on it.

WHERE THINGS LIVE. The endpoint, the model and the switch are an
`assistant:` block in the cluster file, beside `gateway:`. That file is
served to the browser, so the API key is not in it: it is a file of its own
beside it, `assistant.key`, mode 0600, and the browser only ever learns
whether one is set and its last four characters.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

from .cluster import (
    ClusterConfigError,
    NoClusterFileError,
    _atomic_write,
    _current_document,
    dump_cluster,
    parse_cluster,
)
from .prometheus import HISTORY_QUERIES, PrometheusClient

KEY_FILE = "assistant.key"

#: How long a model gets to answer. A reasoning model on this cluster takes
#: tens of seconds over a long context; a minute and a half covers it.
ANSWER_TIMEOUT_S = 90.0
PROBE_TIMEOUT_S = 15.0

SYSTEM_PROMPT = (
    "You answer questions about a small cluster of NVIDIA GB10 machines from its "
    "monitoring data, which follows the question. Use only that data. When it does "
    "not answer the question, say so and say what would. Name the node, the time "
    "(UTC) and the figure behind each claim. Be brief: a few sentences, or a short "
    "list when there are several causes. GB10 has unified memory: the GPU and CPU "
    "share one pool, so memory pressure on a node affects both."
)


#: BB4. The whole of a question with tools: rounds and the answer. Longer
#: than a one-shot, and safe behind a proxy because the answer is streamed
#: (see the ask route). Measured 2026-10-16 with ComfyUI holding sparky's GPU
#: at 96%: Qwen 3.6 35B took over a minute for its first round alone.
TOOLS_TIMEOUT_S = 180.0

#: BB4. How many times the model may call tools before it must answer, and
#: how many calls one turn may make. Measured 2026-10-16 with Qwen 3.6 35B
#: through the gateway: 2-4 s a round once loaded and the node otherwise idle.
MAX_ROUNDS = 6
MAX_CALLS_PER_ROUND = 4
#: A tool's answer, cut to this many characters before the model sees it.
MAX_TOOL_CHARS = 12_000
#: Kept back from every tool round for the final answer, so a slow round
#: (the model's node busy with other work) ends in an answer from what was
#: gathered rather than in a timeout.
FINAL_RESERVE_S = 25.0

TOOL_NOTE = (
    " You can call tools to look further than the data below: a finer step around a moment, "
    "a series it leaves out, another window to compare. For a slowdown or a spike, try "
    "correlate first. Call only what the question needs, then answer."
)


@dataclass(frozen=True)
class AssistantConfig:
    url: str
    model: str
    enabled: bool = True
    #: BB4: let the model call the read-only tools. On unless switched off;
    #: an endpoint that refuses tools gets the one-shot form instead.
    tools: bool = True


def normalize_url(raw: object) -> str:
    """An OpenAI-compatible base: `http://host:port/v1`. A bare host:port gets
    `/v1`; a path is kept, for endpoints mounted elsewhere."""
    hint = "give an OpenAI-compatible address, like http://<host>:4000/v1"
    if not isinstance(raw, str) or not raw.strip():
        raise ClusterConfigError(f"assistant.url is missing; {hint}")
    text = raw.strip()
    if "://" not in text:
        text = f"http://{text}"
    parsed = urlparse(text)
    try:
        parsed.port  # noqa: B018 — raises on a bad port
    except ValueError as exc:
        raise ClusterConfigError(f"assistant.url has a bad port: {raw!r}") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ClusterConfigError(f"assistant.url is not an http(s) URL: {raw!r}; {hint}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ClusterConfigError("assistant.url must be a plain address; the key is set separately")
    path = parsed.path.rstrip("/") or "/v1"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def parse_assistant(payload: object) -> AssistantConfig | None:
    if not isinstance(payload, dict) or payload.get("assistant") is None:
        return None
    block = payload["assistant"]
    if not isinstance(block, dict):
        raise ClusterConfigError("`assistant:` must be a mapping with `url:` and `model:`")
    model = block.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ClusterConfigError("assistant.model is missing")
    flags = {}
    for name in ("enabled", "tools"):
        value = block.get(name, True)
        if not isinstance(value, bool):
            raise ClusterConfigError(f"assistant.{name} must be true or false, not {value!r}")
        flags[name] = value
    return AssistantConfig(url=normalize_url(block.get("url")), model=model.strip(), **flags)


def load_assistant(path: Path) -> AssistantConfig | None:
    try:
        payload = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, yaml.YAMLError) as exc:
        raise ClusterConfigError(f"could not read {path}: {exc}") from exc
    return parse_assistant(payload)


def write_assistant(path: Path, config: AssistantConfig | None) -> None:
    """Set the block, or remove it with None, leaving the rest of the file alone."""
    doc = _current_document(path)
    if doc is None or not isinstance(doc.get("nodes"), list):
        raise NoClusterFileError(
            f"the assistant is configured in the cluster file ({path.name}), and this "
            "deployment does not have one yet. Nodes still come from SPARK_NODES; "
            "move them into the cluster file first."
        )
    nodes = parse_cluster(path.read_text())
    updated = dict(doc)
    if config is None:
        updated.pop("assistant", None)
    else:
        updated["assistant"] = {"url": config.url, "model": config.model, "enabled": config.enabled}
        if not config.tools:
            updated["assistant"]["tools"] = False
    text = dump_cluster(nodes, preserve=updated)
    parse_assistant(yaml.safe_load(text))
    _atomic_write(path, text)


# --- the key, in a file of its own -------------------------------------------

def key_path(cluster_path: Path) -> Path:
    return cluster_path.parent / KEY_FILE


def read_key(cluster_path: Path) -> str | None:
    try:
        return key_path(cluster_path).read_text().strip() or None
    except OSError:
        return None


def write_key(cluster_path: Path, key: str | None) -> None:
    """Owner-only, written atomically. None removes it."""
    p = key_path(cluster_path)
    if key is None:
        p.unlink(missing_ok=True)
        return
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key.strip() + "\n")
    os.replace(tmp, p)


def key_hint(key: str | None) -> str | None:
    """What Settings may show: the last four characters, never more."""
    return f"…{key[-4:]}" if key and len(key) >= 8 else ("set" if key else None)


# --- talking to the endpoint ---------------------------------------------------

#: Names the assistant on the gateway's Clients card, where it would
#: otherwise read as httpx from the monitoring host.
USER_AGENT = f"spark-dash-assistant/{os.environ.get('BACKEND_VERSION', 'dev')}"


def _headers(key: str | None) -> dict:
    headers = {"User-Agent": USER_AGENT}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


async def list_models(url: str, key: str | None) -> list[str]:
    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
        resp = await client.get(f"{url}/models", headers=_headers(key))
    if resp.status_code in (401, 403):
        raise ClusterConfigError(f"the endpoint refused the key ({resp.status_code})")
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return sorted({str(m.get("id")) for m in data if isinstance(m, dict) and m.get("id")})


class EndpointRefused(ClusterConfigError):
    """The endpoint answered with an error status other than a refused key."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"the endpoint answered {status}: {detail}")
        self.status = status


async def _chat(url: str, key: str | None, body: dict, timeout_s: float) -> dict:
    """One chat completion; the first choice's message."""
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(f"{url}/chat/completions", headers=_headers(key),
                                 json={**body, "stream": False})
    if resp.status_code in (401, 403):
        raise ClusterConfigError(f"the endpoint refused the key ({resp.status_code})")
    if resp.status_code >= 400:
        raise EndpointRefused(resp.status_code, resp.text[:300])
    choice = (resp.json().get("choices") or [{}])[0]
    return choice.get("message") or {}


async def complete(url: str, key: str | None, model: str, messages: list[dict],
                   *, max_tokens: int, timeout_s: float) -> tuple[str, float]:
    """One chat completion; (text, seconds). Reasoning text, which some engines
    return in a field of its own, is not part of the answer."""
    started = time.monotonic()
    message = await _chat(url, key, {"model": model, "messages": messages, "max_tokens": max_tokens},
                          timeout_s)
    return (message.get("content") or "").strip(), time.monotonic() - started


async def test(url: str, key: str | None, model: str) -> dict:
    text, seconds = await complete(
        url, key, model,
        [{"role": "user", "content": "Reply with the single word: ready"}],
        max_tokens=256, timeout_s=ANSWER_TIMEOUT_S,
    )
    return {"ok": True, "reply": text[:200], "seconds": round(seconds, 2)}


# --- the context a question is answered from -----------------------------------

#: The series summarised for the window, per node: the ones a "why was it
#: slow / hot / stuck" question is usually about.
SUMMARISED = (
    "gpu_utilization", "gpu_temperature", "gpu_power", "memory_used_percent",
    "cpu_utilization", "psi_some_avg10", "psi_full_avg10", "system_temperature",
    "tokens_per_second", "prompt_tokens_per_second",
)


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M")


def _gib(b) -> str:
    return f"{b / 2**30:.1f}" if isinstance(b, (int, float)) else "?"


def _node_line(n) -> str:
    d = n.model_dump(mode="json")
    g, m, c, p = d.get("gpu") or {}, d.get("memory") or {}, d.get("cpu") or {}, d.get("psi") or {}
    parts = [
        f"{d['node_id']}{' (cluster ' + d['cluster'] + ')' if d.get('cluster') else ''}:"
        f" {'up' if d.get('up') else 'DOWN'}, health {d.get('health')}"
        + (f" ({'; '.join(d['health_reasons'])})" if d.get("health_reasons") else ""),
        f"GPU {g.get('util_pct')}% {g.get('temp_c')}°C {g.get('power_w')}W {g.get('clock_state')}",
        f"memory {_gib(m.get('available_bytes'))} of {_gib(m.get('total_bytes'))} GiB free,"
        f" swap {_gib(m.get('swap_used_bytes'))} GiB",
        f"CPU {c.get('util_pct')}% {c.get('temp_c')}°C, memory pressure {p.get('state')}"
        f" (some {p.get('some_avg10')}%, full {p.get('full_avg10')}%)",
    ]
    procs = sorted(d.get("processes") or [], key=lambda x: -(x.get("gpu_mem_bytes") or 0))[:5]
    if procs:
        parts.append("GPU processes: " + ", ".join(
            f"{x.get('name')} {x.get('runtime') or ''} {x.get('model') or ''} {_gib(x.get('gpu_mem_bytes'))} GiB".replace("  ", " ")
            for x in procs))
    rt = d.get("runtimes") or {}
    for router in rt.get("llama_cpp") or []:
        live = [f"{x['name']} ({x['state']})" for x in router.get("models") or [] if x.get("state") in ("loaded", "sleeping")]
        parts.append(f"llama.cpp {router.get('name') or router.get('endpoint')}: {', '.join(live) or 'nothing loaded'}")
    for runtime in ("vllm", "sglang", "tensorfold"):
        for e in rt.get(runtime) or []:
            parts.append(f"{runtime} {e.get('model')}: {e.get('requests_running')} running, "
                         f"{e.get('requests_waiting')} waiting, {e.get('generation_tokens_per_sec')} tok/s")
    return "\n  ".join(parts)


async def _series_summary(prom: PrometheusClient, start: float, end: float) -> list[str]:
    step = max(60, int((end - start) / 60))
    lines = []

    async def one(key: str):
        expr = HISTORY_QUERIES[key].replace("{window}", "5m")
        try:
            series = await prom.query_range(expr, start, end, f"{step}s")
        except Exception:  # noqa: BLE001 — a missing series is a shorter context, not an error
            return
        per = []
        for s in series:
            pts = [(t, v) for t, v in s.points if v is not None and not math.isnan(v)]
            if not pts:
                continue
            values = [v for _, v in pts]
            peak_t, peak = max(pts, key=lambda tv: tv[1])
            per.append(f"{s.node or s.labels.get('node', '?')} {min(values):.1f}/"
                       f"{sum(values) / len(values):.1f}/{peak:.1f} (peak {_clock(peak_t)})")
        if per:
            lines.append(f"{key}: " + "; ".join(per))

    await asyncio.gather(*(one(k) for k in SUMMARISED if k in HISTORY_QUERIES))
    return sorted(lines)


def _client_lines(clients: dict) -> list[str]:
    """The Clients card's rows, one line each, busiest first."""
    lines = []
    for r in clients.get("rows", [])[:15]:
        h = r["harness"]
        who = h["name"] + (f" ({h['detail']})" if h.get("detail") else "")
        engine = r.get("engine") or {}
        where = f"{engine.get('node')} {engine.get('runtime')}" if engine.get("node") else (engine.get("server") or "no engine")
        tokens = (f", tokens {r['tokens_in']} in / {r['tokens_out']} out"
                  if r.get("tokens_in") is not None else "")
        stale = ", model NOT in its engine's list" if r.get("listed") is False else ""
        lines.append(f"{who} from {r['client']['name']}: {r.get('model') or 'no such model'} on {where}: "
                     f"{r['requests']} requests, {r['per_min']}/min, {r['failed']} failed{tokens}{stale}")
    return lines


async def gather_context(prom: PrometheusClient, snapshot_nodes, *, minutes: int,
                         episodes, annotations, latency: dict | None,
                         clients: dict | None = None) -> tuple[str, dict]:
    """The text sent with the question, and how much of each kind went in."""
    end = time.time()
    start = end - minutes * 60
    sections = [f"Window: {_clock(start)} to {_clock(end)} UTC ({minutes} minutes). Now is {_clock(end)} UTC."]

    sections.append("NODES NOW:\n  " + "\n  ".join(_node_line(n) for n in snapshot_nodes))

    try:
        firing = await prom.query('ALERTS{alertstate="firing"}')
    except Exception:  # noqa: BLE001
        firing = []
    sections.append("ALERTS FIRING NOW: " + (", ".join(
        f"{s.labels.get('alertname')} ({s.labels.get('severity', '?')}, {s.labels.get('node', 'cluster')})"
        for s in firing) or "none"))

    ep_lines = [
        f"{e.alertname} ({e.severity}, {e.node or 'cluster'}): {_clock(e.started_at)} to "
        f"{'ongoing' if e.ongoing else _clock(e.ended_at)}{'' if e.fired else ', pending only'}"
        for e in episodes
    ]
    sections.append("ALERT HISTORY IN WINDOW:\n  " + ("\n  ".join(ep_lines[-40:]) or "none"))

    ev_lines = [f"{_clock(a.ts)} {a.kind}: {a.label}{' (' + a.node + ')' if a.node else ''}" for a in annotations]
    sections.append("EVENTS IN WINDOW (deploys, cold starts, maintenance, alerts):\n  " + ("\n  ".join(ev_lines[-60:]) or "none"))

    if latency and latency.get("models"):
        sections.append("TIME TO FIRST TOKEN, last 15 minutes:\n  " + "\n  ".join(
            f"{m['model']}: p50 {m['p50_s']}s, p95 {m['p95_s']}s, {m['per_min']} req/min"
            for m in latency["models"] if m.get("p50_s") is not None) or "TIME TO FIRST TOKEN: no requests")

    client_lines = _client_lines(clients) if clients else []
    if clients is not None:
        since = clients.get("tokens_since")
        head = "CLIENTS through the gateway in window, busiest first"
        if since:
            head += f" (token counts only since {_clock(since)})"
        sections.append(f"{head}:\n  " + ("\n  ".join(client_lines) or "no requests"))

    series = await _series_summary(prom, start, end)
    sections.append("SERIES IN WINDOW, per node min/avg/max:\n  " + ("\n  ".join(series) or "none"))

    counts = {"nodes": len(snapshot_nodes), "alerts_firing": len(firing), "alert_episodes": len(ep_lines),
              "events": len(ev_lines), "series": len(series), "clients": len(client_lines)}
    return "\n\n".join(sections), counts


def _question(question: str, context: str) -> str:
    return f"{question.strip()}\n\n--- MONITORING DATA ---\n{context}"


async def ask(config: AssistantConfig, key: str | None, question: str, context: str) -> dict:
    text, seconds = await complete(
        config.url, key, config.model,
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": _question(question, context)}],
        max_tokens=2048, timeout_s=ANSWER_TIMEOUT_S,
    )
    return {"answer": text, "model": config.model, "seconds": round(seconds, 1), "tools_used": []}


#: (name, arguments) -> (text, is_error). The app passes the MCP server's own
#: call, so Ask and an outside agent run one set of tools.
CallTool = Callable[[str, dict], Awaitable[tuple[str, bool]]]


def openai_tools(mcp_tools) -> list[dict]:
    """The MCP server's tool list in the OpenAI `tools` shape."""
    return [{"type": "function", "function": {
        "name": t.name, "description": t.description or "",
        "parameters": t.input_schema or {"type": "object", "properties": {}},
    }} for t in mcp_tools]


async def ask_with_tools(config: AssistantConfig, key: str | None, question: str, context: str,
                         tools: list[dict], call: CallTool) -> dict:
    """The bundle as before, plus tools. The model may call them for up to
    MAX_ROUNDS turns inside TOOLS_TIMEOUT_S; then, or when time runs
    short, it is asked for its answer with no tools offered. An endpoint that
    refuses tools on the first turn gets the one-shot form instead."""
    started = time.monotonic()
    deadline = started + TOOLS_TIMEOUT_S
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT + TOOL_NOTE},
        {"role": "user", "content": _question(question, context)},
    ]
    used: list[dict] = []
    for round_ in range(MAX_ROUNDS):
        left = deadline - time.monotonic() - FINAL_RESERVE_S
        if left < 5:
            break
        try:
            message = await _chat(config.url, key, {"model": config.model, "messages": messages,
                                                    "tools": tools, "max_tokens": 2048}, left)
        except httpx.TimeoutException:
            # Out of time for this round: answer from what is already in.
            # On the first round there is nothing in yet, so it is a
            # timeout like any other.
            if round_ == 0:
                raise
            break
        except EndpointRefused as exc:
            # A 4xx, or a 5xx that names tools (llama.cpp without --jinja),
            # on the first turn: the endpoint does not take tools.
            if round_ == 0 and (exc.status < 500 or "tool" in str(exc).lower()):
                answer = await ask(config, key, question, context)
                return {**answer, "tools_note": f"the endpoint refused tools, so it was asked without: {exc}"}
            raise
        calls = message.get("tool_calls") or []
        if not calls:
            return {"answer": (message.get("content") or "").strip(), "model": config.model,
                    "seconds": round(time.monotonic() - started, 1), "tools_used": used}
        calls = [{"id": c.get("id") or f"call_{round_}_{i}", "type": "function",
                  "function": c.get("function") or {}} for i, c in enumerate(calls[:MAX_CALLS_PER_ROUND])]
        # Back as the model sent it, less any reasoning text, which some
        # engines refuse to be sent.
        messages.append({"role": "assistant", "content": message.get("content") or "",
                         "tool_calls": calls})
        for c in calls:
            fn = c.get("function") or {}
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments must be an object")
            except ValueError as exc:
                text, error, args = f"could not read the arguments: {exc}", True, {}
            else:
                text, error = await call(name, args)
            if len(text) > MAX_TOOL_CHARS:
                text = text[:MAX_TOOL_CHARS] + f"\n[cut at {MAX_TOOL_CHARS} characters; ask for less]"
            used.append({"tool": name, "args": args, "error": error, "chars": len(text)})
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": text})

    left = max(10.0, deadline - time.monotonic())
    messages.append({"role": "user", "content": "Answer the question now, from what you have."})
    message = await _chat(config.url, key, {"model": config.model, "messages": messages,
                                            "max_tokens": 2048}, left)
    return {"answer": (message.get("content") or "").strip(), "model": config.model,
            "seconds": round(time.monotonic() - started, 1), "tools_used": used}
