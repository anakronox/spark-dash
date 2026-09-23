"""The two rules the DGX OS updater keeps, wherever it is called from.

This file used to be a seam. Until AL3g there were two things that could
answer the dashboard's `/api/fleet*` routes -- a proxy to a separate
`spark-fleet-updates` container, and the engine in this process -- and a
`FleetBackend` Protocol here named what both had to implement. The proxy went
on 2026-09-23 once a real update had run embedded, and a Protocol with one
implementation is a shape without a reason, so it went too.

What is left is the part that was never about the transport.

1. **A password is accepted only over TLS.** Every call that can carry one
   takes `secure`: the truth about the channel the request arrived on --
   cloudflared's `X-Forwarded-Proto` through the tunnel, else the scheme this
   server saw. Never assumed, never defaulted to true.

2. **A body is never logged.** An update request carries the user's sudo
   password for that Spark. It is held for one run and written nowhere: not
   to `state.json`, not to a log line, not into an exception's detail.
"""

from __future__ import annotations


class FleetError(Exception):
    """A refusal to show the person, with the status to send it under.

    `detail` is what appears in the panel, so it says what actually happened
    -- "sparketa is already being updated", the HTTPS refusal -- and never a
    stack trace or an echo of the request body.
    """

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
