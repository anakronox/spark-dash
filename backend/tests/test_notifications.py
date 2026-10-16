"""Notifications through ntfy (roadmap BF): the topic file, its capability
states, and the routes."""

from __future__ import annotations

import os
import stat

import pytest
from fastapi.testclient import TestClient

from spark_dash_backend import notifications
from spark_dash_backend.app import create_app
from spark_dash_backend.config import Settings

NODES = "nodes:\n- id: sparky\n  host: 127.0.0.1\n  agent_port: 9\n"


class TestUrls:
    def test_a_topic_gets_the_alertmanager_template(self):
        assert notifications.build_url("https://ntfy.sh", "spark-dash-abc") == \
            "https://ntfy.sh/spark-dash-abc?template=alertmanager"
        assert notifications.build_url("ntfy.example.com:8443/", "t") == \
            "https://ntfy.example.com:8443/t?template=alertmanager"

    @pytest.mark.parametrize("server, topic", [("ftp://x", "t"), ("https://ntfy.sh", "a/b"), ("https://ntfy.sh", "")])
    def test_refused(self, server, topic):
        with pytest.raises(ValueError):
            notifications.build_url(server, topic)

    def test_a_new_topic_is_hard_to_guess(self):
        a, b = notifications.new_topic(), notifications.new_topic()
        assert a != b and a.startswith("spark-dash-") and len(a) >= 27
        assert not notifications.describe(notifications.build_url("https://ntfy.sh", a))["guessable"]

    def test_describe_flags_a_guessable_topic_and_a_missing_template(self):
        d = notifications.describe("https://ntfy.sh/spark-dash")
        assert d["guessable"] and not d["template"] and d["public_server"]
        assert notifications.describe("https://n.example/spark-dash-k7mq2vx9wp4n?template=alertmanager")["template"]


class TestCapability:
    def test_not_mounted(self, tmp_path):
        assert notifications.NtfyFile(tmp_path / "nope").capability()["missing"] == "mount"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
    def test_an_unreadable_file_and_an_unwritable_dir(self, tmp_path):
        f = notifications.NtfyFile(tmp_path)
        f.path.write_text("https://ntfy.sh/x")
        f.path.chmod(0o200)
        assert f.capability()["missing"] == "file_permissions"
        f.path.chmod(0o600)
        tmp_path.chmod(0o500)
        try:
            assert f.capability()["missing"] == "dir_permissions"
        finally:
            tmp_path.chmod(0o700)

    def test_written_atomically_and_group_readable(self, tmp_path):
        f = notifications.NtfyFile(tmp_path)
        f.write("https://ntfy.sh/t?template=alertmanager")
        assert f.read() == "https://ntfy.sh/t?template=alertmanager"
        assert stat.S_IMODE(os.stat(f.path).st_mode) == 0o640
        assert not list(tmp_path.glob(".*.tmp"))


@pytest.fixture
def api(tmp_path, monkeypatch):
    async def ok(self):
        return True

    async def nothing(self, expr, at=None):
        return []

    async def routing(self):
        return {"default": "ntfy", "receivers": {"ntfy": ["webhook"]}}

    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.healthy", ok)
    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.query", nothing)
    monkeypatch.setattr("spark_dash_backend.alerts.AlertmanagerClient.reachable", ok)
    monkeypatch.setattr("spark_dash_backend.alerts.AlertmanagerClient.routing", routing)
    cluster = tmp_path / "cluster" / "cluster.yml"
    cluster.parent.mkdir()
    cluster.write_text(NODES)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (tmp_path / "targets").mkdir()
    app = create_app(Settings(cluster_config=cluster, notifications_dir=secrets,
                              prometheus_targets_dir=tmp_path / "targets",
                              static_dir=tmp_path / "nostatic", agent_timeout_s=0.5))
    with TestClient(app) as client:
        client.secrets = secrets
        yield client


def test_a_new_topic_is_written_shown_once_asked_and_never_in_the_status(api):
    assert api.get("/api/notifications/status").json()["configured"] is False
    body = api.put("/api/notifications/topic", json={}).json()
    topic = body["topic"]
    assert topic.startswith("spark-dash-") and body["web"] == f"https://ntfy.sh/{topic}"
    assert (api.secrets / "ntfy-url").read_text() == f"https://ntfy.sh/{topic}?template=alertmanager"
    status = api.get("/api/notifications/status").json()
    assert status["configured"] and topic not in str(status)
    assert status["topic_masked"].startswith("spark-dash-") and "•" in status["topic_masked"]
    assert status["alertmanager"]["default_receiver"] == "ntfy"
    assert api.get("/api/notifications/topic").json()["topic"] == topic


def test_an_own_server_and_topic(api):
    body = api.put("/api/notifications/topic", json={"server": "https://ntfy.example.com",
                                                     "topic": "spark-dash-k7mq2vx9wp4n"}).json()
    assert body["server"] == "https://ntfy.example.com" and body["public_server"] is False


def test_the_test_sends_an_alertmanager_shaped_notification(api, monkeypatch):
    sent = {}

    class Resp:
        status_code = 200
        text = ""

    async def post(self, url, json=None, headers=None):
        sent.update(url=url, json=json)
        return Resp()

    monkeypatch.setattr("httpx.AsyncClient.post", post)
    assert api.post("/api/notifications/test").status_code == 409
    api.put("/api/notifications/topic", json={"topic": "spark-dash-k7mq2vx9wp4n"})
    r = api.post("/api/notifications/test").json()
    assert r["ok"] and sent["url"].endswith("?template=alertmanager")
    assert sent["json"]["alerts"][0]["labels"]["alertname"] == "SparkDashTest"


def test_without_the_mount_it_says_so_and_refuses(api, tmp_path):
    import shutil
    shutil.rmtree(api.secrets)
    status = api.get("/api/notifications/status").json()
    assert status["capability"]["missing"] == "mount"
    assert api.put("/api/notifications/topic", json={}).status_code == 409
