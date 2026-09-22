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

from spark_dash_backend.fleet_api import FleetError

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

    # ---------------------------------------------------------- FleetBackend
    #
    # The intents from fleet_api, each one request on the wire. Thin on
    # purpose: this half of the seam is scheduled for deletion at AL3g, and
    # anything clever written here is work that gets thrown away.

    @property
    def embedded(self) -> bool:
        return False

    async def _call(
        self, method: str, path: str, *, secure: bool, body: Any = None
    ) -> dict[str, Any]:
        try:
            status, payload = await self.forward(
                method, path, json=body, proto="https" if secure else "http"
            )
        except FleetUpdatesError as exc:
            raise FleetError(502, f"spark-fleet-updates: {exc}") from exc
        if status >= 400:
            # Its wording, not ours -- "already being updated", the HTTPS
            # refusal. The panel shows what the thing that refused said.
            detail = payload.get("error") if isinstance(payload, dict) else None
            raise FleetError(status, detail or f"spark-fleet-updates answered {status}")
        return payload

    async def envelope(self, *, secure: bool) -> dict[str, Any]:
        return await self._call("GET", "/api/fleet", secure=secure)

    async def log(self, run_id: str, node: str, *, secure: bool) -> dict[str, Any]:
        return await self._call("GET", f"/api/runs/{run_id}/{node}/log", secure=secure)

    async def check_all(self, *, secure: bool) -> dict[str, Any]:
        return await self._call("POST", "/api/check", secure=secure, body={})

    async def enrol(self, name: str, host: str, *, secure: bool) -> dict[str, Any]:
        return await self._call(
            "POST", "/api/nodes", secure=secure, body={"name": name, "host": host}
        )

    async def node_action(
        self, name: str, action: str, *, password: str | None, secure: bool
    ) -> dict[str, Any]:
        return await self._call(
            "POST",
            f"/api/nodes/{name}/{action}",
            secure=secure,
            body={"password": password} if password else {},
        )

    async def run_action(self, run_id: str, action: str, *, secure: bool) -> dict[str, Any]:
        return await self._call("POST", f"/api/runs/{run_id}/{action}", secure=secure, body={})
