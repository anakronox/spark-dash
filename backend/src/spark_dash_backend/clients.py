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

from spark_dash_backend.cluster import GatewayConfig
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
