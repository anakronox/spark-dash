"""Firing alerts, read from Alertmanager.

Alertmanager rather than Prometheus's own `ALERTS` metric, because it knows
things Prometheus doesn't: what's been silenced, what's been inhibited, and how
alerts group together. Showing a suppressed alert in the dashboard would defeat
the point of having silenced it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

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
