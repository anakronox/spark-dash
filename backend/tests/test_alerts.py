"""Alert surfacing.

Read from Alertmanager rather than Prometheus's ALERTS metric, because only
Alertmanager knows what's been silenced or inhibited — showing a silenced alert
would defeat the point of silencing it.
"""

import httpx
import pytest
from spark_dash_backend.alerts import AlertmanagerClient, _parse

AM_ALERT = {
    "labels": {
        "alertname": "GpuThrottled",
        "severity": "critical",
        "node": "sparky",
        "state": "THROTTLED",
    },
    "annotations": {
        "summary": "GPU on sparky throttled under sustained load",
        "description": "Clock has stayed below the throttle threshold.",
    },
    "startsAt": "2026-08-14T09:00:00.000Z",
}

AM_WARNING = {
    "labels": {"alertname": "MemoryPressureHigh", "severity": "warning", "node": "spark2"},
    "annotations": {"summary": "Memory pressure HIGH on spark2", "description": "Stalling."},
    "startsAt": "2026-08-14T09:05:00.000Z",
}


def client_with(handler) -> AlertmanagerClient:
    c = AlertmanagerClient("http://alertmanager:9093")
    # Patch the transport the client builds internally.
    import spark_dash_backend.alerts as mod

    original = mod.httpx.AsyncClient
    mod.httpx.AsyncClient = lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    c._restore = lambda: setattr(mod.httpx, "AsyncClient", original)  # type: ignore[attr-defined]
    return c


async def test_parses_an_alert():
    alert = _parse(AM_ALERT)
    assert alert.name == "GpuThrottled"
    assert alert.severity == "critical"
    assert alert.node == "sparky"
    assert "throttled" in alert.summary


async def test_missing_annotations_do_not_raise():
    """A rule without annotations is a bug, but it must not break the page."""
    alert = _parse({"labels": {"alertname": "Bare"}})
    assert alert.name == "Bare"
    assert alert.summary == ""
    assert alert.severity == "info"


async def test_firing_sorts_critical_first():
    """A rendered list should lead with what matters."""

    def handler(request):
        return httpx.Response(200, json=[AM_WARNING, AM_ALERT])

    c = client_with(handler)
    try:
        alerts = await c.firing()
    finally:
        c._restore()

    assert [a.severity for a in alerts] == ["critical", "warning"]


async def test_only_active_unsilenced_alerts_are_requested():
    """Silenced and inhibited alerts must not reach the dashboard."""
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=[])

    c = client_with(handler)
    try:
        await c.firing()
    finally:
        c._restore()

    assert seen["active"] == "true"
    assert seen["silenced"] == "false"
    assert seen["inhibited"] == "false"


async def test_unreachable_alertmanager_yields_empty_not_an_exception():
    """The page must still render; `reachable()` is how the UI distinguishes
    'nothing wrong' from 'can't tell'."""

    def handler(request):
        raise httpx.ConnectError("refused")

    c = client_with(handler)
    try:
        assert await c.firing() == []
        assert await c.reachable() is False
    finally:
        c._restore()


async def test_reachable_true_on_healthy():
    def handler(request):
        return httpx.Response(200, text="OK")

    c = client_with(handler)
    try:
        assert await c.reachable() is True
    finally:
        c._restore()


async def test_garbage_payload_is_skipped():
    def handler(request):
        return httpx.Response(200, json=["not a dict", AM_ALERT])

    c = client_with(handler)
    try:
        alerts = await c.firing()
    finally:
        c._restore()

    assert len(alerts) == 1


@pytest.mark.parametrize("severity,expected", [("critical", 0), ("warning", 1), ("info", 2)])
def test_severity_ordering(severity, expected):
    from spark_dash_backend.alerts import SEVERITY_ORDER

    assert SEVERITY_ORDER[severity] == expected
