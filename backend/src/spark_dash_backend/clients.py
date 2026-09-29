"""Client stats from a LiteLLM gateway (roadmap AM).

The engines count requests per model and never record a caller. A LiteLLM
proxy in front of them does: its Prometheus metrics carry `user_agent`,
`client_ip` and `api_base` on every request. Prometheus scrapes the gateway
(the `litellm` job, target rendered by `inventory.render_gateway_file_sd`), and
this module only ever asks Prometheus. It never talks to an engine, and of the
gateway itself it reads nothing but `/health/liveliness` and `/metrics`:
every LiteLLM health check is a real completion, and on a llama.cpp router
with autoload that loads the model.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from urllib.parse import urlparse

import httpx

from spark_dash_backend.cluster import GatewayConfig, authority, normalize_gateway_url
from spark_dash_backend.prometheus import PrometheusClient, PrometheusError

log = logging.getLogger(__name__)


async def gateway_health(
    gateway: GatewayConfig | None, error: str | None, prom: PrometheusClient
) -> str:
    """The `litellm` entry in /health.

    `not scraped` covers both a target Prometheus has not picked up yet (file_sd
    refreshes every 30 s) and one it cannot reach; `up` is what tells them
    apart, and the dashboard does not need to.
    """
    if error:
        return f"invalid: {error}"
    if gateway is None:
        return "not configured"
    if not gateway.enabled:
        return "off"
    try:
        series = await prom.query('up{job="litellm"}')
    except PrometheusError:
        # Prometheus being down is already reported under its own key.
        return "unknown"
    return "ok" if any(s.points and s.points[-1][1] == 1 for s in series) else "not scraped"


#: The ONLY paths the Test button requests on a gateway, in order. `/health`
#: is not among them and must never be: on LiteLLM it sends a real completion
#: to every configured model. `test_clients.py` asserts the exact list.
PROBE_PATHS = ("/health/liveliness", "/metrics")

#: Test seam, as `llama_router.py` has one: lets the suite record every URL
#: the probe requests.
probe_transport: httpx.AsyncBaseTransport | None = None


async def probe_gateway(url: str, *, timeout_s: float = 5.0) -> dict:
    """Settings' Test button: is this a LiteLLM gateway the dashboard can read?

    Unsaved on purpose, so a URL can be tried before it is written. Returns a
    sentence for each outcome, since the person reading it is deciding what to
    change and where.
    """
    base = normalize_gateway_url(url)
    result: dict = {"url": base, "reachable": False, "metrics": "not checked", "detail": ""}
    async with httpx.AsyncClient(
        timeout=timeout_s, transport=probe_transport, follow_redirects=True
    ) as client:
        try:
            alive = await client.get(f"{base}{PROBE_PATHS[0]}")
        except httpx.HTTPError as exc:
            result["detail"] = f"No answer from {base} ({type(exc).__name__})."
            return result
        if alive.status_code != 200:
            result["detail"] = (
                f"{base} answered {alive.status_code} on {PROBE_PATHS[0]}. "
                "Is this a LiteLLM proxy?"
            )
            return result
        result["reachable"] = True

        try:
            metrics = await client.get(f"{base}{PROBE_PATHS[1]}")
        except httpx.HTTPError as exc:
            result["metrics"] = "error"
            result["detail"] = f"The gateway answered, but /metrics did not ({type(exc).__name__})."
            return result

    if urlparse(str(metrics.url)).netloc != urlparse(base).netloc:
        # LiteLLM redirects /metrics to /metrics/ on the same host. Anything
        # that leaves the host is not the gateway answering.
        result["metrics"] = "error"
        result["detail"] = f"/metrics redirected away from the gateway, to {metrics.url.host}."
    elif metrics.status_code in (401, 403):
        result["metrics"] = "needs a key"
        result["detail"] = (
            "The gateway asks for a key on /metrics. The dashboard reads it "
            "without one: see require_auth_for_metrics_endpoint in LiteLLM's "
            "Prometheus docs."
        )
    elif metrics.status_code != 200:
        result["metrics"] = "error"
        result["detail"] = f"/metrics answered {metrics.status_code}."
    elif "# TYPE litellm_" not in metrics.text:
        result["metrics"] = "missing"
        result["detail"] = (
            "/metrics answered but carries no LiteLLM series. Is the gateway's "
            "Prometheus callback on?"
        )
    else:
        result["metrics"] = "ok"
        result["detail"] = "Reachable, and its metrics are readable."
    return result


async def gateway_status(inventory, prom: PrometheusClient) -> dict:
    """What Settings renders. Asks Prometheus, never the gateway.

    `state` is /health's value: `ok` means Prometheus is scraping it, which
    already proves it reachable and its metrics readable without a key. So the
    status can be polled freely without sending the gateway a request.
    """
    gateway, error = inventory.gateway()
    on = bool(gateway and gateway.enabled)
    return {
        # Capability is a URL, use is the toggle: AL5's two levels (AM2).
        "capability": gateway is not None,
        "enabled": on,
        "configured": on,
        "url": gateway.url if gateway else None,
        "error": error,
        "cluster_file": inventory.cluster_file_present,
        "state": await gateway_health(gateway, error, prom),
    }


# --- rows (roadmap AM4b) -------------------------------------------------------

REQUESTS = "litellm_deployment_total_requests_total"
FAILURES = "litellm_proxy_failed_requests_metric_total"
UP = 'up{job="litellm"}'

#: What one row of the card is. `api_base` is not in it: a model's prefix
#: names one endpoint, and the failure metric does not carry `api_base` at all.
ROW_KEY = ("user_agent", "client_ip", "requested_model")


def counted(metric: str, by: str, window: str) -> str:
    """Requests in the window, counted correctly for series that start inside it.

    `increase()` cannot see a counter's first value, so a client that appears
    mid-window and makes one request would count as 0 and vanish. Three
    cases, which cannot overlap:

    - the series existed when the window opened: `increase()`, which also
      handles a LiteLLM restart resetting it;
    - it appeared later, and Prometheus was already scraping the gateway then:
      it is genuinely new, so its whole value is in the window;
    - it appeared later because the SCRAPE started later: its value includes
      requests from before anyone was counting, so only what Prometheus saw
      counts (latest minus first). `counting_since` tells the card.

    Checked against the live gateway 2026-09-29: a counter that read 14 at the
    first scrape and 23 later came out as 9. The naive `increase()` said 8.31,
    and adding the whole value back regardless said 21.
    """
    return (
        f"sum by ({by}) ("
        f"(increase({metric}[{window}]) and {metric} offset {window})"
        f" or ((max_over_time({metric}[{window}]) unless {metric} offset {window})"
        f" and on() ({UP} offset {window}))"
        f" or ((max_over_time({metric}[{window}]) - min_over_time({metric}[{window}])"
        f" unless {metric} offset {window}) unless on() ({UP} offset {window}))"
        ")"
    )


#: User-Agent prefix -> (name, kind). Only strings actually seen, each with a
#: test; anything else falls back to the product token (`opencode/1.2` reads
#: as "opencode") until it is confirmed and added here.
#:
#: kind: `harness` names what is calling; `sdk` is a library's default, which
#: many harnesses and scripts send unchanged, so it cannot say which one (the
#: Client column usually can); `tool` is a command-line or bare HTTP client.
HARNESSES: tuple[tuple[str, str, str], ...] = (
    ("claude-cli/", "Claude Code", "harness"),
    ("AsyncOpenAI/Python", "OpenAI SDK (Python)", "sdk"),
    ("OpenAI/Python", "OpenAI SDK (Python)", "sdk"),
    ("OpenAI/JS", "OpenAI SDK (JS)", "sdk"),
    ("AsyncAnthropic/Python", "Anthropic SDK (Python)", "sdk"),
    ("Anthropic/Python", "Anthropic SDK (Python)", "sdk"),
    ("Anthropic/JS", "Anthropic SDK (JS)", "sdk"),
    ("litellm/", "LiteLLM SDK", "sdk"),
    ("curl/", "curl", "tool"),
    ("python-httpx/", "httpx", "tool"),
    ("python-requests/", "requests", "tool"),
)


def classify(user_agent: str | None, route: str | None = None) -> dict:
    """What is calling, from its User-Agent."""
    if not user_agent or user_agent == "None":
        # LiteLLM drops the User-Agent on a failed /v1/messages request, so
        # Claude Code's errors arrive unattributed; the route says which API
        # it was speaking. Measured 2026-09-29.
        if route == "/v1/messages":
            return {"name": "Anthropic-API client", "kind": "unattributed"}
        return {"name": "unknown", "kind": "unattributed"}
    for prefix, name, kind in HARNESSES:
        if user_agent.startswith(prefix):
            return {"name": name, "kind": kind}
    product = user_agent.split("/", 1)[0].split(" ", 1)[0].strip()
    return {"name": product or user_agent, "kind": "unknown"}


class Names:
    """Reverse DNS for client IPs and forward DNS for configured hosts, cached.

    Lookups run in a thread with a short timeout: a LAN without PTR records
    must cost the card half a second once an hour per address, not a stall on
    every poll. Failures are cached too, as None.
    """

    TTL_S = 3600.0
    TIMEOUT_S = 0.5

    def __init__(self, reverse=None, forward=None) -> None:
        self._reverse = reverse or (lambda ip: socket.gethostbyaddr(ip)[0])
        self._forward = forward or socket.gethostbyname
        self._cache: dict[tuple[str, str], tuple[float, str | None]] = {}

    async def _lookup(self, kind: str, key: str) -> str | None:
        hit = self._cache.get((kind, key))
        if hit and hit[0] > time.monotonic():
            return hit[1]
        fn = self._reverse if kind == "ptr" else self._forward
        try:
            value = await asyncio.wait_for(asyncio.to_thread(fn, key), self.TIMEOUT_S)
        except Exception:  # noqa: BLE001 — no name is an answer, not an error
            value = None
        self._cache[(kind, key)] = (time.monotonic() + self.TTL_S, value)
        return value

    async def hostname(self, ip: str) -> str | None:
        return await self._lookup("ptr", ip)

    async def address(self, host: str) -> str:
        """`host` as an IP, or unchanged if it is one or does not resolve."""
        return await self._lookup("a", host) or host


def _split(authority: str) -> tuple[str, str]:
    host, _, port = authority.rpartition(":")
    return (host, port) if host else (authority, "")


async def endpoint_index(cluster_nodes, names: Names) -> dict[str, dict]:
    """`ip:port` -> the node and runtime serving there, from cluster.yml.

    Keyed by IP because either side may be written as a hostname: the gateway's
    `api_base` and the cluster file are configured separately.
    """
    index: dict[str, dict] = {}
    for node in cluster_nodes:
        entries = [(r.url, "llama.cpp") for r in node.runtimes.llama_routers]
        for runtime, urls in node.runtimes.engines.items():
            entries += [(u, runtime) for u in urls]
        for url, runtime in entries:
            host, port = _split(authority(url))
            key = f"{await names.address(host)}:{port}"
            index[key] = {"server": authority(url), "node": node.node_id, "runtime": runtime}
    return index


async def client_rows(prom: PrometheusClient, cluster_nodes, names: Names, minutes: int) -> dict:
    """The Clients card: who called what, in the last `minutes`."""
    window = f"{minutes}m"
    by_request = "user_agent, client_ip, api_base, requested_model"
    by_failure = "user_agent, client_ip, requested_model, exception_status, route"
    requests, rates, failures, scraped_then, first_scrape = await asyncio.gather(
        prom.query(counted(REQUESTS, by_request, window)),
        prom.query(f"sum by ({by_request}) (rate({REQUESTS}[5m]))"),
        prom.query(counted(FAILURES, by_failure, window)),
        prom.query(f"{UP} offset {window}"),
        prom.query(f"min_over_time(timestamp({UP})[{window}:15s])"),
    )

    rows: dict[tuple, dict] = {}

    def row(labels: dict) -> dict:
        key = tuple(labels.get(k, "") for k in ROW_KEY)
        if key not in rows:
            rows[key] = {
                "user_agent": labels.get("user_agent") or None,
                "client_ip": labels.get("client_ip") or None,
                "requested_model": labels.get("requested_model") or None,
                "api_base": None,
                "requests": 0.0,
                "per_min": 0.0,
                "failed": 0.0,
                "statuses": {},
                "routes": set(),
            }
        return rows[key]

    for s in requests:
        r = row(s.labels)
        r["requests"] += s.points[-1][1] if s.points else 0.0
        r["api_base"] = r["api_base"] or s.labels.get("api_base") or None
    for s in rates:
        r = row(s.labels)
        r["per_min"] += (s.points[-1][1] if s.points else 0.0) * 60
        r["api_base"] = r["api_base"] or s.labels.get("api_base") or None
    for s in failures:
        value = s.points[-1][1] if s.points else 0.0
        if value <= 0:
            continue
        r = row(s.labels)
        r["failed"] += value
        status = s.labels.get("exception_status") or "?"
        r["statuses"][status] = r["statuses"].get(status, 0) + round(value)
        if s.labels.get("route"):
            r["routes"].add(s.labels["route"])

    index = await endpoint_index(cluster_nodes, names)
    node_hosts = {await names.address(n.host): n.node_id for n in cluster_nodes}

    out = []
    for r in rows.values():
        requests_n, failed_n = round(r["requests"]), round(r["failed"])
        if not requests_n and not failed_n and r["per_min"] <= 0:
            continue  # quiet in this window
        ip = r["client_ip"]
        fqdn = await names.hostname(ip) if ip else None
        engine = None
        if r["api_base"]:
            host, port = _split(authority(r["api_base"]))
            key = f"{await names.address(host)}:{port}"
            engine = index.get(key) or {
                "server": authority(r["api_base"]),
                "node": None,
                "runtime": None,
            }
        routes = sorted(r["routes"])
        rejected = r["requested_model"] == "other"
        out.append({
            "harness": classify(r["user_agent"], routes[0] if len(routes) == 1 else None),
            "user_agent": r["user_agent"],
            "client": {
                "ip": ip,
                "node": node_hosts.get(ip),
                "name": node_hosts.get(ip) or (fqdn.split(".", 1)[0] if fqdn else ip),
                "fqdn": fqdn,
            },
            # "other" is LiteLLM's label for a request no route matched: it
            # was refused at the gateway and reached no engine.
            "model": None if rejected else r["requested_model"],
            "rejected": rejected,
            "engine": engine,
            "requests": requests_n,
            "failed": failed_n,
            "statuses": r["statuses"],
            "routes": routes,
            "per_min": round(r["per_min"], 2),
            "active": r["per_min"] > 0,
        })
    out.sort(key=lambda x: (-x["requests"], -x["failed"], x["client"]["name"] or ""))

    counting_since = None
    if not scraped_then and first_scrape and first_scrape[0].points:
        counting_since = first_scrape[0].points[-1][1]
    return {"window_minutes": minutes, "counting_since": counting_since, "rows": out}
