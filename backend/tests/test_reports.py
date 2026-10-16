"""Reports (roadmap BI): when schedules fire, building, rendering, keeping,
and the routes."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
import yaml
from fastapi.testclient import TestClient

from spark_dash_backend import agent_tools, reports
from spark_dash_backend.app import create_app
from spark_dash_backend.cluster import ClusterConfigError
from spark_dash_backend.config import Settings
from spark_dash_backend.prometheus import Series

NY = ZoneInfo("America/New_York")
NODES = "nodes:\n- id: sparky\n  host: 127.0.0.1\n  agent_port: 9\n"


def sched(**kw) -> reports.Schedule:
    return reports.parse_schedule({"name": "t", "frequency": "daily", **kw})


def at(s: str, tz=NY) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=tz)


class TestNextDue:
    def test_daily_later_today_or_tomorrow(self):
        s = sched(time="08:00")
        assert reports.next_due(s, at("2026-10-16T07:59"), NY) == at("2026-10-16T08:00")
        assert reports.next_due(s, at("2026-10-16T08:00"), NY) == at("2026-10-17T08:00")

    def test_daily_keeps_wall_clock_time_across_daylight_saving(self):
        """US clocks go back on 2026-11-01: 08:00 stays 08:00 local, so the
        UTC instant moves by an hour."""
        s = sched(time="08:00")
        before = reports.next_due(s, at("2026-10-31T07:00"), NY)  # still summer time
        after = reports.next_due(s, before, NY)  # the first morning after the change
        assert before.hour == after.hour == 8
        # Compared in UTC: subtracting two datetimes that share a tzinfo
        # takes their wall-clock difference and ignores the offset.
        assert (after.astimezone(UTC) - before.astimezone(UTC)).total_seconds() == 25 * 3600

    def test_weekly_on_its_day(self):
        s = sched(frequency="weekly", weekday="mon", time="09:30")
        d = reports.next_due(s, at("2026-10-16T12:00"), NY)  # a Friday
        assert d == at("2026-10-19T09:30") and d.weekday() == 0

    def test_monthly_past_a_short_months_end_is_its_last_day(self):
        s = sched(frequency="monthly", day=31, time="06:00")
        assert reports.next_due(s, at("2026-02-01T00:00"), NY) == at("2026-02-28T06:00")
        assert reports.next_due(s, at("2026-02-28T06:00"), NY) == at("2026-03-31T06:00")

    def test_every_n_hours_on_whole_hours_from_midnight(self):
        s = sched(frequency="hours", every_hours=6)
        assert reports.next_due(s, at("2026-10-16T07:12"), NY) == at("2026-10-16T12:00")
        assert reports.next_due(s, at("2026-10-16T23:59"), NY) == at("2026-10-17T00:00")

    def test_periods(self):
        assert reports.period_start(sched(), at("2026-10-16T08:00")) == at("2026-10-15T08:00")
        m = sched(frequency="monthly", day=31)
        assert reports.period_start(m, at("2026-03-31T06:00")) == at("2026-02-28T06:00")


class TestDue:
    def config(self, **kw):
        return reports.ReportsConfig(schedules=(sched(id="a", time="08:00", **kw),))

    def test_a_new_schedule_does_not_fire_at_once(self):
        assert reports.due_runs(self.config(), {}, at("2026-10-16T12:00").astimezone(UTC), NY) == []

    def test_due_after_its_time(self):
        state = {"a": {"since": at("2026-10-16T07:00").isoformat()}}
        assert reports.due_runs(self.config(), state, at("2026-10-16T07:59").astimezone(UTC), NY) == []
        [(s, due)] = reports.due_runs(self.config(), state, at("2026-10-16T08:01").astimezone(UTC), NY)
        assert due == at("2026-10-16T08:00")

    def test_several_missed_runs_run_once_for_the_latest(self):
        state = {"a": {"last_due": at("2026-10-10T08:00").isoformat()}}
        runs = reports.due_runs(self.config(), state, at("2026-10-16T09:00").astimezone(UTC), NY)
        assert [d for _, d in runs] == [at("2026-10-16T08:00")]

    def test_disabled_never_runs(self):
        state = {"a": {"last_due": at("2026-10-10T08:00").isoformat()}}
        assert reports.due_runs(self.config(enabled=False), state, at("2026-10-16T09:00").astimezone(UTC), NY) == []


class TestConfig:
    def test_bad_values_are_refused(self):
        for bad in ({"frequency": "fortnightly"}, {"time": "25:00"}, {"weekday": "xyz"},
                    {"day": 0}, {"sections": ["nope"]}):
            with pytest.raises(ClusterConfigError):
                sched(**bad)
        with pytest.raises(ClusterConfigError):
            reports.parse_reports({"reports": {"timezone": "Mars/Olympus"}})
        with pytest.raises(ClusterConfigError):
            reports.parse_reports({"reports": {"public_url": "monitor:8080"}})

    def test_an_unquoted_time_in_a_hand_edit_still_reads(self):
        """YAML 1.1 reads `time: 10:30` as 630."""
        raw = yaml.safe_load("frequency: daily\ntime: 10:30\n")
        assert raw["time"] == 630
        assert reports.parse_schedule(raw).time == "10:30"

    def test_round_trips_through_the_cluster_file_keeping_the_nodes(self, tmp_path):
        p = tmp_path / "cluster.yml"
        p.write_text(NODES)
        cfg = reports.ReportsConfig(timezone="Europe/London", public_url="http://monitor:8080",
                                    schedules=(sched(id="w", frequency="weekly", weekday="fri"),))
        reports.write_reports(p, cfg)
        doc = yaml.safe_load(p.read_text())
        assert doc["nodes"][0]["id"] == "sparky"
        back = reports.load_reports(p)
        assert back.timezone == "Europe/London" and back.schedules[0].weekday == "fri"


def test_host_timezone_order(tmp_path, monkeypatch):
    f = tmp_path / "timezone"
    f.write_text("Europe/Moscow\n")
    none = tmp_path / "absent"
    monkeypatch.delenv("TZ", raising=False)
    assert reports.host_timezone(f, none) == "Europe/Moscow"
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert reports.host_timezone(f, none) == "Asia/Tokyo"


def test_the_host_zone_from_localtime_content_prefers_a_region_name(tmp_path, monkeypatch):
    """A bind mount of /etc/localtime gives the file, not the link's name;
    Moscow's data is also W-SU's."""
    import zoneinfo
    from pathlib import Path

    src = next((Path(r) / "Europe" / "Moscow" for r in zoneinfo.TZPATH if (Path(r) / "Europe" / "Moscow").exists()), None)
    if src is None:
        pytest.skip("no system zoneinfo files here")
    lt = tmp_path / "localtime"
    lt.write_bytes(src.read_bytes())
    monkeypatch.delenv("TZ", raising=False)
    assert reports.host_timezone(tmp_path / "absent", lt) == "Europe/Moscow"


# --- building and rendering --------------------------------------------------------------

class Prom:
    async def query(self, expr, at=None):
        return []

    async def query_range(self, expr, start, end, step):
        st = int(step.rstrip("s"))
        n = int((end - start) // st)
        if "gpu_utilization" in expr:
            return [Series(labels={"node": "sparky"}, points=[(start + i * st, 90.0 if i == 3 else 10.0)
                                                              for i in range(n + 1)])]
        if "gpu_process_sm_percent" in expr:
            return [Series(labels={"node": "sparky", "runtime": "comfyui"}, points=[(start, 40.0), (end, 20.0)])]
        return []

    async def data_age_s(self):
        return 1.0


def sources() -> agent_tools.Sources:
    async def none(*_):
        return None

    async def no_nodes():
        return []

    async def lat():
        return {"models": []}

    return agent_tools.Sources(prom=Prom(), nodes=no_nodes, latency=lat, clients=none, promql_enabled=lambda: False)


def built(**kw) -> dict:
    end = datetime(2026, 10, 16, 20, 0, tzinfo=UTC).timestamp()
    import time as _t
    end = min(end, _t.time())
    return asyncio.run(reports.build(sources(), start=end - 86400, end=end, tz="Europe/Moscow", **kw))


def test_a_report_has_its_sections_in_order_and_a_headline():
    doc = built(sections=["workloads", "overview", "health"])
    assert [s["id"] for s in doc["sections"]] == ["workloads", "overview", "health"]
    over = doc["sections"][1]
    row = over["tables"][0]["rows"][0]
    assert row["node"] == "sparky" and row["gpu_peak"] == 90.0
    assert over["lines"][0].startswith("Busiest: sparky")
    assert any("comfyui" in h for h in doc["headline"])
    assert doc["window"]["tz"] == "Europe/Moscow"


def test_times_are_in_the_reports_zone_with_the_date():
    doc = built(sections=["overview"])
    at_ = doc["sections"][0]["tables"][0]["rows"][0]["gpu_peak_at"]
    assert len(at_) == 11 and at_[2] == "-" and at_[5] == " "  # MM-DD HH:MM


def test_a_section_that_fails_says_so_and_the_rest_stand(monkeypatch):
    async def boom(ctx):
        raise RuntimeError("nope")

    monkeypatch.setitem(reports.BUILDERS, "models", boom)
    doc = built(sections=["overview", "models"])
    assert doc["sections"][1]["error"] and "nope" in doc["sections"][1]["lines"][0]
    assert doc["sections"][0]["tables"][0]["rows"]


def test_refused_periods_and_sections():
    with pytest.raises(ValueError):
        asyncio.run(reports.build(sources(), start=10, end=5, sections=["overview"]))
    with pytest.raises(ValueError):
        asyncio.run(reports.build(sources(), start=0, end=40 * 86400, sections=["overview"]))
    with pytest.raises(ValueError):
        asyncio.run(reports.build(sources(), start=0, end=60, sections=["nope"]))


def test_the_narrative_is_marked_as_the_models_and_a_failure_leaves_the_report():
    async def narrate(md):
        assert "# Cluster report" in md
        return {"text": "A quiet day.", "model": "qwen"}

    doc = built(sections=["overview"], narrate=narrate)
    assert doc["narrative"] == {"text": "A quiet day.", "model": "qwen"}
    assert "Summary, written by qwen" in reports.render_markdown(doc)
    assert "Summary, written by qwen" in reports.render_html(doc)

    async def down(md):
        raise RuntimeError("timeout")

    doc = built(sections=["overview"], narrate=down)
    assert "error" in doc["narrative"] and doc["sections"][0]["tables"]


def test_renderings():
    doc = built(sections=["overview", "workloads", "clients"])
    md = reports.render_markdown(doc)
    assert "| Node | GPU % avg |" in md and "| sparky |" in md
    page = reports.render_html(doc, auto_print=True)
    assert page.startswith("<!doctype html>") and "@media print" in page and "print()" in page
    assert "<script" not in reports.render_html(doc)
    z = zipfile.ZipFile(io.BytesIO(reports.render_csv_zip(doc)))
    names = z.namelist()
    assert "01-overview-per-node.csv" in names and "report.json" in names
    assert z.read("01-overview-per-node.csv").decode().startswith("Node,GPU % avg")


def test_html_escapes_what_it_shows():
    doc = built(sections=["overview"], title="<script>x</script>")
    assert "<script>x</script>" not in reports.render_html(doc)


class TestStore:
    def test_keep_list_get_delete_and_prune(self, tmp_path):
        store = reports.ReportStore(tmp_path / "cluster.yml")
        docs = [built(sections=["overview"]) for _ in range(3)]
        for i, d in enumerate(docs):
            d["id"] = f"2026101{i}-000000-abc"
            store.save(d)
        assert [r["id"] for r in store.list()] == [d["id"] for d in reversed(docs)]
        assert store.get(docs[0]["id"])["title"] == docs[0]["title"]
        assert store.prune(keep_count=2, keep_days=365) == 1
        assert len(store.list()) == 2
        store.delete(docs[2]["id"])
        assert len(store.list()) == 1

    def test_ids_cannot_reach_outside_the_directory(self, tmp_path):
        store = reports.ReportStore(tmp_path / "cluster.yml")
        for bad in ("../cluster", "a/b", "", "x.json"):
            with pytest.raises(KeyError):
                store.get(bad)


# --- routes -------------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    async def ok(self):
        return True

    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.healthy", ok)
    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.query", Prom.query)
    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.query_range", Prom.query_range)
    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.data_age_s", Prom.data_age_s)
    cluster = tmp_path / "cluster" / "cluster.yml"
    cluster.parent.mkdir()
    cluster.write_text(NODES)
    (tmp_path / "targets").mkdir()
    app = create_app(Settings(cluster_config=cluster, notifications_dir=tmp_path / "nosecrets",
                              prometheus_targets_dir=tmp_path / "targets",
                              static_dir=tmp_path / "nostatic", agent_timeout_s=0.5))
    with TestClient(app) as client:
        client.cluster = cluster
        yield client


def test_build_keep_list_download_delete(api):
    r = api.post("/api/reports/build", json={"minutes": 60, "sections": ["overview", "health"], "keep": True})
    assert r.status_code == 200, r.text
    doc = r.json()
    rid = doc["id"]
    assert doc["kept"] and [x["id"] for x in api.get("/api/reports").json()["reports"]] == [rid]
    for fmt, kind in (("md", "text/markdown"), ("html", "text/html"), ("csv", "application/zip"),
                      ("json", "application/json")):
        d = api.get(f"/api/reports/{rid}/download", params={"format": fmt})
        assert d.status_code == 200 and d.headers["content-type"].startswith(kind)
    assert "print()" in api.get(f"/api/reports/{rid}/download", params={"format": "html", "print": 1}).text
    assert api.delete(f"/api/reports/{rid}").status_code == 200
    assert api.get(f"/api/reports/{rid}").status_code == 404


def test_built_but_not_kept_until_asked(api):
    doc = api.post("/api/reports/build", json={"minutes": 60, "sections": ["overview"]}).json()
    assert api.get("/api/reports").json()["reports"] == []
    assert api.post("/api/reports/keep", json=doc).json()["kept"] == doc["id"]
    assert len(api.get("/api/reports").json()["reports"]) == 1


def test_config_meta_and_run_now(api):
    meta = api.get("/api/reports/meta").json()
    assert meta["config"]["schedules"] == [] and meta["timezone"]
    assert {s["id"] for s in meta["sections"]} == set(reports.SECTIONS)
    assert next(s for s in meta["sections"] if s["id"] == "clients")["available"] is False
    body = {"timezone": "Europe/London", "keep_count": 50, "keep_days": 30, "public_url": "http://monitor:8080",
            "schedules": [{"id": "d1", "name": "Daily", "frequency": "daily", "time": "07:30",
                           "sections": ["overview"], "notify": False}]}
    meta = api.put("/api/reports/config", json=body).json()
    assert meta["timezone"] == "Europe/London" and meta["config"]["schedules"][0]["time"] == "07:30"
    assert api.put("/api/reports/config", json={"schedules": [{"frequency": "never"}]}).status_code == 400
    run = api.post("/api/reports/schedules/d1/run").json()
    kept = api.get(f"/api/reports/{run['id']}").json()
    assert kept["schedule"] == {"id": "d1", "name": "Daily"} and kept["title"].startswith("Daily, ")
    assert api.post("/api/reports/schedules/nope/run").status_code == 404


def test_agents_can_list_get_and_build(api):
    api.put("/api/mcp/config", json={"enabled": True})
    key = agent_tools.read_key(api.cluster)
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
         "MCP-Protocol-Version": "2025-06-18", "Authorization": f"Bearer {key}"}

    def call(name, args):
        r = api.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                               "params": {"name": name, "arguments": args}}).json()
        return r["result"]["content"][0]["text"]

    md = call("build_report", {"sections": ["overview"], "minutes": 60})
    assert md.startswith("# Cluster report") and "| sparky |" in md
    rid = api.post("/api/reports/build", json={"minutes": 60, "sections": ["overview"], "keep": True}).json()["id"]
    assert json.loads(call("list_reports", {}))["reports"][0]["id"] == rid
    assert call("get_report", {"report_id": rid}).startswith("# Cluster report")
    assert json.loads(call("get_report", {"report_id": rid, "format": "json"}))["id"] == rid
