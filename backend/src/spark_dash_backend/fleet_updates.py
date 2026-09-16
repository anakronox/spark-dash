"""The fleet updater, reached through the backend (roadmap AK).

spark-fleet-updates is its own service: it holds the SSH key, does the hourly
checks and runs the update state machine. The dashboard holds none of that.
What it has is a window onto the fleet service's JSON API, and this is the
pane of glass: every call the panel makes is forwarded here, with the
incoming request's scheme attached, and the answer handed back unchanged.

TWO THINGS THIS MUST NOT DO.

Never log a body. An update request carries the user's sudo password for
that Spark; the fleet service keeps it in memory for one run and never
writes it anywhere, and the proxy in front of it has to be at least that
careful. Transport errors are logged by class and message only.

Never vouch for a channel it did not see. The fleet service accepts a
password only over TLS or when a proxy says `X-Forwarded-Proto: https`.
This forwards the REAL scheme -- the header cloudflared set through the
tunnel, or else the scheme the backend itself received -- so a password
typed into the dashboard on the plain-HTTP LAN is refused by the fleet
service exactly as it would be on the fleet service's own page. That rule
is the fleet service's to relax (SPARK_FLEET_ALLOW_PLAIN_PASSWORD), not
this proxy's to bypass.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class FleetUpdatesError(Exception):
    """The fleet service could not be reached, or answered with no JSON."""


class FleetUpdatesClient:
    def __init__(self, base_url: str, *, timeout_s: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        # Swapped by tests for an httpx.MockTransport; None means the default.
        self._transport: httpx.AsyncBaseTransport | None = None

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    async def forward(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        proto: str,
    ) -> tuple[int, Any]:
        """Send one request on and return `(status, decoded body)`.

        Raises `FleetUpdatesError` when the service is unreachable or answers
        something that is not JSON. An HTTP error status is NOT raised: the
        fleet service explains refusals in `{"error": ...}` with a 4xx, and
        that wording is what the panel should show.
        """
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                resp = await client.request(
                    method,
                    f"{self._base_url}{path}",
                    json=json,
                    headers={"X-Forwarded-Proto": proto},
                )
                return resp.status_code, resp.json()
        except httpx.HTTPError as exc:
            log.debug("fleet-updates %s %s failed: %s", method, path, type(exc).__name__)
            raise FleetUpdatesError(f"{type(exc).__name__}: {exc}") from exc
        except ValueError as exc:  # not JSON
            raise FleetUpdatesError(
                "fleet service answered with something other than JSON"
            ) from exc

    async def reachable(self) -> bool:
        if not self.configured:
            return False
        try:
            status, _ = await self.forward("GET", "/api/fleet", proto="http")
        except FleetUpdatesError:
            return False
        return status == 200
