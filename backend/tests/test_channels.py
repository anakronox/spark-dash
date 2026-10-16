"""Slack, Discord and Telegram (roadmap BJ): channels, messages, filters,
delivery, and the routes."""

from __future__ import annotations

import asyncio
import json
import os
import stat

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from spark_dash_backend import channels
from spark_dash_backend.app import create_app
from spark_dash_backend.cluster import ClusterConfigError
from spark_dash_backend.config import Settings

NODES = "nodes:\n- id: sparky\n  host: 127.0.0.1\n  agent_port: 9\n"

FIRING = {
    "status": "firing", "commonLabels": {"alertname": "GpuHot", "severity": "critical", "node": "sparky"},
    "alerts": [{"status": "firing", "labels": {"alertname": "GpuHot", "severity": "critical", "node": "sparky"},
                "annotations": {"summary": "GPU on sparky above its critical band",
                                "description": "86 °C against a limit of 90."}}],
}


def ch(**kw) -> channels.Channel:
    return channels.parse_channel({"type": "slack", **kw})


class TestParse:
    def test_refused(self):
        for bad in ({"type": "email"}, {"type": "slack", "alerts": "some"}, {"type": "telegram"}):
            with pytest.raises(ClusterConfigError):
                channels.parse_channel(bad)

    def test_secrets_are_checked_for_shape(self):
        assert channels.check_secret("slack", "https://hooks.slack.com/services/T/B/X")
        with pytest.raises(ClusterConfigError):
            channels.check_secret("discord", "http://discord.com/api/webhooks/1/x")
        assert channels.check_secret("telegram", "123456789:AAAbbbCCCdddEEEfffGGG")
        with pytest.raises(ClusterConfigError):
            channels.check_secret("telegram", "not-a-token")


class TestMessages:
    def test_an_alert_names_itself_its_node_and_why(self):
        m = channels.from_alertmanager(FIRING, "http://monitor:8080")
        assert m.title == "Critical: GpuHot on sparky"
        assert m.lines[0] == "GPU on sparky above its critical band"
        assert "86 °C" in m.lines[1] and m.severity == "critical" and not m.resolved

    def test_a_group_counts_its_alerts_and_a_recovery_says_so(self):
        p = {**FIRING, "status": "resolved", "commonLabels": {"alertname": "GpuHot"},
             "alerts": FIRING["alerts"] * 2}
        m = channels.from_alertmanager(p, None)
        assert m.title.startswith("Resolved: GpuHot") and m.title.endswith("(2)")
        assert m.resolved and m.severity == "critical"

    def test_filters(self):
        crit = channels.from_alertmanager(FIRING, None)
        warn = channels.Message(title="x", severity="warning")
        back = channels.Message(title="x", severity="critical", resolved=True)
        report = channels.Message(title="r", kind="report")
        assert channels.wants(ch(alerts="all"), warn)
        assert not channels.wants(ch(alerts="critical"), warn)
        assert channels.wants(ch(alerts="critical"), crit)
        assert channels.wants(ch(alerts="critical"), back), "a critical channel hears a critical one end"
        assert not channels.wants(ch(resolved=False), back)
        assert not channels.wants(ch(alerts="off"), crit)
        assert channels.wants(ch(alerts="off", reports=True), report)
        assert not channels.wants(ch(reports=False), report)
        assert not channels.wants(ch(enabled=False), crit)


def capture(status: int = 200):
    sent = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append({"url": str(req.url), "json": json.loads(req.content)})
        return httpx.Response(status, text="no" if status >= 300 else "ok")

    return sent, httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestSend:
    def test_slack_discord_and_telegram_each_in_their_own_shape(self):
        m = channels.from_alertmanager(FIRING, "http://monitor:8080")
        sent, client = capture()
        asyncio.run(channels.send(ch(), "https://hooks.slack.com/x", m, client=client))
        asyncio.run(channels.send(channels.parse_channel({"type": "discord"}), "https://discord.com/api/webhooks/1/y",
                                  m, client=client))
        tg = channels.parse_channel({"type": "telegram", "chat_id": "-100123"})
        asyncio.run(channels.send(tg, "123456789:AAAbbbCCCdddEEEfffGGG", m, client=client))
        slack, discord, telegram = sent
        assert slack["json"]["attachments"][0]["color"] == "#d85b5b"
        assert slack["json"]["attachments"][0]["title_link"] == "http://monitor:8080"
        assert discord["json"]["embeds"][0]["color"] == 0xD85B5B and discord["json"]["username"] == "spark-dash"
        assert telegram["url"].endswith("/bot123456789:AAAbbbCCCdddEEEfffGGG/sendMessage")
        assert telegram["json"]["chat_id"] == "-100123" and telegram["json"]["parse_mode"] == "HTML"
        assert "<b>Critical: GpuHot on sparky</b>" in telegram["json"]["text"]

    def test_telegram_escapes_what_it_quotes(self):
        m = channels.Message(title="<script>", lines=["a & b"])
        sent, client = capture()
        tg = channels.parse_channel({"type": "telegram", "chat_id": "1"})
        asyncio.run(channels.send(tg, "123456789:AAAbbbCCCdddEEEfffGGG", m, client=client))
        assert "&lt;script&gt;" in sent[0]["json"]["text"] and "a &amp; b" in sent[0]["json"]["text"]

    def test_a_refusal_is_reported_without_the_secret(self):
        sent, client = capture(403)
        r = asyncio.run(channels.send(ch(), "https://hooks.slack.com/SECRET", channels.Message(title="t"),
                                      client=client))
        assert r == {"ok": False, "status": 403, "detail": "no"}


def test_the_webhook_token_is_made_once_group_readable(tmp_path):
    t1 = channels.ensure_webhook_token(tmp_path)
    t2 = channels.ensure_webhook_token(tmp_path)
    assert t1 and t1 == t2
    assert stat.S_IMODE(os.stat(tmp_path / channels.WEBHOOK_TOKEN_FILE).st_mode) == 0o640
    assert channels.ensure_webhook_token(tmp_path / "missing") is None


# --- routes -----------------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    async def ok(self):
        return True

    async def routing(self):
        return {"default": "ntfy", "receivers": {"ntfy": ["webhook"], "spark-dash": ["webhook"]}}

    async def nothing(self, expr, at=None):
        return []

    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.healthy", ok)
    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.query", nothing)
    monkeypatch.setattr("spark_dash_backend.alerts.AlertmanagerClient.reachable", ok)
    monkeypatch.setattr("spark_dash_backend.alerts.AlertmanagerClient.routing", routing)
    cluster = tmp_path / "cluster" / "cluster.yml"
    cluster.parent.mkdir()
    cluster.write_text(NODES)
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (tmp_path / "targets").mkdir()
    sent = []

    async def fake_send(c, secret, m, *, client=None):
        sent.append({"channel": c.id, "secret": secret, "title": m.title, "kind": m.kind})
        return {"ok": True, "status": 200, "detail": None}

    monkeypatch.setattr(channels, "send", fake_send)
    app = create_app(Settings(cluster_config=cluster, notifications_dir=secrets_dir,
                              prometheus_targets_dir=tmp_path / "targets",
                              static_dir=tmp_path / "nostatic", agent_timeout_s=0.5))
    with TestClient(app) as client:
        client.cluster, client.secrets_dir, client.sent = cluster, secrets_dir, sent
        yield client


SLACK = {"id": "s1", "type": "slack", "name": "#ops", "alerts": "critical",
         "secret": "https://hooks.slack.com/services/T/B/abcd1234"}


def test_channels_save_with_their_secrets_kept_apart(api):
    body = api.put("/api/channels", json={"channels": [SLACK]}).json()
    c = body["channels"][0]
    assert c["secret_set"] and c["secret_hint"] == "…1234" and "hooks.slack.com" not in json.dumps(body)
    assert yaml.safe_load(api.cluster.read_text())["channels"][0]["alerts"] == "critical"
    assert "hooks" not in api.cluster.read_text()
    p = api.cluster.parent / channels.SECRETS_FILE
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    # Saved again without the secret: kept. Removed: the secret goes too.
    api.put("/api/channels", json={"channels": [{**SLACK, "secret": None, "name": "#alerts"}]})
    assert channels.read_secrets(api.cluster)["s1"].endswith("abcd1234")
    api.put("/api/channels", json={"channels": []})
    assert channels.read_secrets(api.cluster) == {}
    assert "channels" not in yaml.safe_load(api.cluster.read_text())
    assert body["forwarding"] and body["token"]


def test_alertmanager_needs_the_token_and_alerts_are_filtered(api):
    api.put("/api/channels", json={"channels": [SLACK, {"id": "d1", "type": "discord", "alerts": "all",
                                                        "secret": "https://discord.com/api/webhooks/1/x"}]})
    assert api.post("/api/alerts/notify", json=FIRING).status_code == 401
    token = (api.secrets_dir / channels.WEBHOOK_TOKEN_FILE).read_text()
    h = {"Authorization": f"Bearer {token}"}
    r = api.post("/api/alerts/notify", json=FIRING, headers=h).json()
    assert set(r["sent"]) == {"s1", "d1"}
    warning = {**FIRING, "commonLabels": {"alertname": "SwapThrashing", "severity": "warning"}}
    api.sent.clear()
    api.post("/api/alerts/notify", json=warning, headers=h)
    assert [x["channel"] for x in api.sent] == ["d1"], "the critical-only channel skipped a warning"


def test_a_test_message_and_reports_reach_the_channels(api):
    api.put("/api/channels", json={"channels": [SLACK]})
    assert api.post("/api/channels/s1/test").json()["ok"]
    assert api.sent[-1]["kind"] == "test"
    assert api.post("/api/channels/nope/test").status_code == 404
    api.put("/api/reports/config", json={"schedules": [{"id": "d", "name": "Daily", "frequency": "daily",
                                                        "sections": ["health"], "notify": True}]})
    api.sent.clear()
    api.post("/api/reports/schedules/d/run")
    assert [x["kind"] for x in api.sent] == ["report"]
