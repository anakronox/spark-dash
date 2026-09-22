"""What the dashboard can ask of the fleet updater, in one place (AL3c).

There are two things that can answer: the `spark-fleet-updates` container over
HTTP (`fleet_updates.ProxyFleet`, how AK shipped it) and the engine running
inside this process (`fleet_embedded.EmbeddedFleet`, where AL is going). This
module is the seam between them, and it exists so that `app.py` has exactly
one fleet vocabulary rather than a branch in every route.

THE METHODS ARE INTENTS, NOT HTTP. `envelope`, `check_all`, `node_action` --
not `forward("POST", "/api/nodes/...")`. That matters for what comes next: at
AL3g the proxy goes, and what is left should read as the backend calling its
own service, not as a shape left behind by a network hop that no longer
happens.

TWO RULES BOTH IMPLEMENTATIONS KEEP, because they are the feature's safety and
not the transport's:

1. **A password is accepted only over TLS.** Every method that can carry one
   takes `secure`, which is the truth about the channel the request arrived
   on -- cloudflared's `X-Forwarded-Proto` through the tunnel, else the scheme
   this server saw. Never assumed, never defaulted to true. The proxy forwards
   it and lets the fleet service decide; the embedded one applies the same
   rule itself, with the same sentence.
2. **A body is never logged.** An update request carries the user's sudo
   password for that Spark. It is held for one run and written nowhere -- not
   to `state.json`, not to a log line, not into an exception's detail.
"""

from __future__ import annotations

from typing import Any, Protocol


class FleetError(Exception):
    """A refusal to show the person, with the status to send it under.

    `detail` is what appears in the panel, so it is the fleet service's own
    wording where there is one -- "sparketa is already being updated", the
    HTTPS refusal -- and never a stack trace or a body echo.
    """

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class FleetBackend(Protocol):
    """Implemented twice: over HTTP, and in-process."""

    @property
    def configured(self) -> bool:
        """Is there a fleet updater at all? False means no button (AK)."""

    @property
    def embedded(self) -> bool:
        """True when this process is the fleet updater (AL)."""

    async def reachable(self) -> bool:
        """For `/health`. Unreachable is not a problem -- the dashboard is not
        blind without the fleet updater, it just cannot offer it."""

    async def envelope(self, *, secure: bool) -> dict[str, Any]:
        """Every Spark, its posture, its active run: what the panel renders."""

    async def log(self, run_id: str, node: str, *, secure: bool) -> dict[str, Any]:
        """The tail of one node's run log."""

    async def check_all(self, *, secure: bool) -> dict[str, Any]:
        """Collect and score every Spark now. Read-only on the nodes."""

    async def enrol(self, name: str, host: str, *, secure: bool) -> dict[str, Any]:
        """Put a node on the fleet's list by the id and host the dashboard
        already has for it (AK7, the Settings checkbox)."""

    async def node_action(
        self, name: str, action: str, *, password: str | None, secure: bool
    ) -> dict[str, Any]:
        """check, update, rehearse or remove, for one Spark."""

    async def run_action(self, run_id: str, action: str, *, secure: bool) -> dict[str, Any]:
        """stop, or verify a run that was held at the restart."""
