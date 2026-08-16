"""Firing alerts, read from Alertmanager.

Alertmanager rather than Prometheus's own `ALERTS` metric, because it knows
things Prometheus doesn't: what's been silenced, what's been inhibited, and how
alerts group together. Showing a suppressed alert in the dashboard would defeat
the point of having silenced it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

log = logging.getLogger(__name__)

# Worst first, so a rendered list leads with what matters.
SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


@dataclass
class Alert:
    name: str
    severity: str
    summary: str
    description: str
    node: str | None
    started_at: str | None
    labels: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "severity": self.severity,
            "summary": self.summary,
            "description": self.description,
            "node": self.node,
            "started_at": self.started_at,
            "labels": self.labels,
        }


class AlertmanagerClient:
    def __init__(self, base_url: str, *, timeout_s: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    async def firing(self) -> list[Alert]:
        """Active, unsilenced, uninhibited alerts.

        Returns an empty list when Alertmanager is unreachable rather than
        raising. The dashboard's own per-node health still works without it,
        and a broken alerting system shouldn't take the page down — but
        `reachable()` exists so the UI can say alerts are unavailable instead
        of implying all-clear.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.get(
                    f"{self._base_url}/api/v2/alerts",
                    params={
                        "active": "true",
                        "silenced": "false",
                        "inhibited": "false",
                    },
                )
                resp.raise_for_status()
                payload = resp.json()
        except Exception:  # noqa: BLE001 — reported via reachable(), not raised
            log.debug("alertmanager query failed", exc_info=True)
            return []

        alerts = [_parse(item) for item in payload if isinstance(item, dict)]
        alerts.sort(key=lambda a: (SEVERITY_ORDER.get(a.severity, 9), a.name))
        return alerts

    # --- silences ---------------------------------------------------------
    #
    # Silencing is a WRITE, and the dashboard is otherwise read-only. It is
    # allowed because it is a far narrower primitive than the writes that were
    # ruled out: a silence cannot repoint an agent, load a model or touch a
    # process. Its worst case is muted alerts — bounded, and behind the same
    # OAuth the rest of the page is.
    #
    # The reason it exists is workflow, not convenience. This is a box for
    # experimentation: stacks come up and get torn down constantly, and every
    # teardown leaves a target down and an alert firing with no way to say "yes,
    # that was me". An alert you cannot clear is one you learn to ignore, which
    # is worse than no alert.

    async def silences(self) -> list[dict]:
        """Active silences, newest first.

        Surfaced deliberately: a muted alert that is invisible is a way to hide
        problems from yourself. If something is silenced, the dashboard has to
        say so.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.get(f"{self._base_url}/api/v2/silences")
                resp.raise_for_status()
                payload = resp.json()
        except Exception:  # noqa: BLE001 — same reasoning as firing()
            log.debug("silence query failed", exc_info=True)
            return []

        active = [
            s
            for s in payload
            if isinstance(s, dict) and s.get("status", {}).get("state") == "active"
        ]
        active.sort(key=lambda s: s.get("startsAt", ""), reverse=True)
        return active

    async def create_silence(
        self,
        matchers: list[dict],
        *,
        hours: float,
        comment: str,
        author: str = "spark-dash",
    ) -> str:
        """Silence matching alerts for a bounded period. Returns the silence id.

        ALWAYS bounded. The failure mode of silencing is forgetting, so there
        is no indefinite option here — a permanently unwanted alert should have
        its target removed from configuration, which is the honest fix for a
        retired stack rather than a mute that hides a real failure months later.
        """
        now = datetime.now(UTC)
        body = {
            "matchers": matchers,
            "startsAt": now.isoformat(),
            "endsAt": (now + timedelta(hours=hours)).isoformat(),
            "createdBy": author,
            "comment": comment,
        }
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            resp = await client.post(f"{self._base_url}/api/v2/silences", json=body)
            resp.raise_for_status()
            return str(resp.json().get("silenceID", ""))

    async def expire_silence(self, silence_id: str) -> None:
        """End a silence early — the undo for a mute applied by mistake."""
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            resp = await client.delete(f"{self._base_url}/api/v2/silence/{silence_id}")
            resp.raise_for_status()

    async def reachable(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.get(f"{self._base_url}/-/healthy")
                return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False


def _parse(item: dict) -> Alert:
    labels = {k: str(v) for k, v in (item.get("labels") or {}).items()}
    annotations = item.get("annotations") or {}
    return Alert(
        name=labels.get("alertname", "unknown"),
        severity=labels.get("severity", "info"),
        summary=annotations.get("summary", ""),
        description=annotations.get("description", ""),
        node=labels.get("node"),
        started_at=item.get("startsAt"),
        labels=labels,
    )
