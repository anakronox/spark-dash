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

import logging
from urllib.parse import urlparse

import httpx

from spark_dash_backend.cluster import GatewayConfig, normalize_gateway_url
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
