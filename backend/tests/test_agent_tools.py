"""Tools for agents (roadmap BB): the MCP endpoint, its key, the tools, and
Ask's loop over them."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time

import pytest
import yaml
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from spark_dash_backend import agent_tools, assistant
from spark_dash_backend.app import create_app
from spark_dash_backend.config import Settings
from spark_dash_backend.prometheus import HISTORY_QUERIES, Series

NODES = "nodes:\n- id: sparky\n  host: 127.0.0.1\n  agent_port: 9\n"
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
           "MCP-Protocol-Version": "2025-06-18"}


def rpc(method: str, params: dict | None = None, id_: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "method": method, **({"params": params} if params else {})}


@pytest.fixture
def api(tmp_path, monkeypatch):
    async def fake_healthy(self):
        return True

    async def fake_query(self, expr, at=None):
        return []

    async def fake_range(self, expr, start, end, step):
        if expr.startswith("sparkdash_gpu_temperature_celsius"):
            return [Series(labels={"node": "sparky"}, points=[(start, 50.0), (start + 60, 81.0), (end, 60.0)])]
        return []

    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.healthy", fake_healthy)
    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.query", fake_query)
    monkeypatch.setattr("spark_dash_backend.prometheus.PrometheusClient.query_range", fake_range)
    cluster = tmp_path / "cluster" / "cluster.yml"
    cluster.parent.mkdir()
    cluster.write_text(NODES)
    (tmp_path / "targets").mkdir()
    app = create_app(Settings(cluster_config=cluster, prometheus_targets_dir=tmp_path / "targets",
                              static_dir=tmp_path / "nostatic", agent_timeout_s=0.5))
    with TestClient(app) as client:
        client.cluster = cluster
        yield client


def switch_on(api, **flags) -> dict:
    body = api.put("/api/mcp/config", json={"enabled": True, **flags}).json()
    api.headers_with_key = {**HEADERS, "Authorization": f"Bearer {body.get('key') or agent_tools.read_key(api.cluster)}"}
    return body


class TestTheEndpoint:
    def test_off_until_switched_on(self, api):
        assert api.post("/mcp", json=rpc("tools/list"), headers=HEADERS).status_code == 404

    def test_no_key_or_a_wrong_one_is_refused(self, api):
        switch_on(api)
        assert api.post("/mcp", json=rpc("tools/list"), headers=HEADERS).status_code == 401
        wrong = {**HEADERS, "Authorization": "Bearer sdk-nope"}
        assert api.post("/mcp", json=rpc("tools/list"), headers=wrong).status_code == 401

    def test_a_client_can_initialise_and_list_the_tools(self, api):
        switch_on(api)
        init = api.post("/mcp", headers=api.headers_with_key, json=rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}))
        assert init.status_code == 200
        assert "GB10" in init.json()["result"]["instructions"]
        tools = api.post("/mcp", json=rpc("tools/list"), headers=api.headers_with_key).json()["result"]["tools"]
        names = [t["name"] for t in tools]
        assert names == ["cluster_now", "topology", "list_metrics", "metric_history", "compare", "correlate",
                         "alerts", "alerting", "events", "latency", "model_activity", "clients",
                         "monitoring_health", "updates"]

    def test_every_tool_says_it_only_reads(self, api):
        switch_on(api, promql=True)
        tools = api.post("/mcp", json=rpc("tools/list"), headers=api.headers_with_key).json()["result"]["tools"]
        assert "promql" in [t["name"] for t in tools]
        for t in tools:
            assert t["annotations"]["readOnlyHint"] is True, t["name"]
            assert t["annotations"]["destructiveHint"] is False, t["name"]

    def test_a_tool_call_answers_in_json(self, api):
        switch_on(api)
        r = api.post("/mcp", headers=api.headers_with_key, json=rpc("tools/call", {
            "name": "metric_history", "arguments": {"metric": "gpu_temperature", "minutes": 60}})).json()
        body = json.loads(r["result"]["content"][0]["text"])
        assert body["unit"] == "°C"
        assert body["series"][0]["node"] == "sparky"
        assert body["series"][0]["summary"]["max"] == 81.0

    def test_an_unknown_metric_is_an_error_the_model_can_read(self, api):
        switch_on(api)
        r = api.post("/mcp", headers=api.headers_with_key, json=rpc("tools/call", {
            "name": "metric_history", "arguments": {"metric": "nope"}})).json()
        assert r["result"]["isError"] is True
        assert "list_metrics" in r["result"]["content"][0]["text"]


class TestTheKey:
    def test_switching_on_makes_a_key_shown_once(self, api):
        body = switch_on(api)
        key = body["key"]
        assert key.startswith("sdk-") and len(key) > 40
        assert body["key_hint"] == f"…{key[-4:]}"
        status = api.get("/api/mcp/status").json()
        assert "key" not in status and status["key_set"]
        # Switching it off and on again keeps the key, and does not show it.
        api.put("/api/mcp/config", json={"enabled": False})
        assert "key" not in api.put("/api/mcp/config", json={"enabled": True}).json()

    def test_the_key_is_owner_only_and_not_in_the_cluster_file(self, api):
        key = switch_on(api)["key"]
        assert key not in api.cluster.read_text()
        assert yaml.safe_load(api.cluster.read_text())["mcp"] == {"enabled": True, "promql": False, "notes": False}
        path = agent_tools.key_path(api.cluster)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_a_new_key_retires_the_old_one(self, api):
        switch_on(api)
        old = api.headers_with_key
        new = api.post("/api/mcp/key").json()["key"]
        assert api.post("/mcp", json=rpc("tools/list"), headers=old).status_code == 401
        fresh = {**HEADERS, "Authorization": f"Bearer {new}"}
        assert api.post("/mcp", json=rpc("tools/list"), headers=fresh).status_code == 200

    def test_the_nodes_survive(self, api):
        switch_on(api, promql=True)
        api.put("/api/mcp/config", json={"enabled": False, "promql": False})
        doc = yaml.safe_load(api.cluster.read_text())
        assert doc["nodes"][0]["id"] == "sparky" and "mcp" not in doc


def test_every_named_series_is_described():
    """A series added to HISTORY_QUERIES must say its unit and meaning here,
    or an agent is handed a name it cannot read."""
    assert set(agent_tools.METRICS) == set(HISTORY_QUERIES)


class TestTimes:
    NOW = 1_800_000_000.0

    @pytest.mark.parametrize("raw", ["2027-01-15T08:00Z", "2027-01-15T08:00:00+00:00", "2027-01-15 08:00",
                                     "1800000000", "1800000000.0"])
    def test_readable_forms(self, raw):
        assert agent_tools.parse_time(raw, now=self.NOW + 1e6) == self.NOW

    def test_none_and_now_are_now(self):
        assert agent_tools.parse_time(None, now=self.NOW) == self.NOW
        assert agent_tools.parse_time("now", now=self.NOW) == self.NOW

    def test_the_future_is_clamped(self):
        assert agent_tools.parse_time("2099-01-01T00:00Z", now=self.NOW) == self.NOW

    def test_before_retention_and_nonsense_are_refused(self):
        with pytest.raises(ToolError):
            agent_tools.parse_time("2020-01-01T00:00Z", now=self.NOW)
        with pytest.raises(ToolError):
            agent_tools.parse_time("last tuesday", now=self.NOW)

    def test_a_step_keeps_a_range_under_the_cap(self):
        for seconds in (60, 3600, 86400, 31 * 86400):
            step = agent_tools.step_for(seconds)
            assert step % 15 == 0 and step >= 15
            assert seconds / step <= agent_tools.MAX_POINTS


class Prom:
    """Flat everywhere, except what a test sets."""

    def __init__(self, moves: dict[tuple[str, str], tuple[float, float]] | None = None):
        self.moves = moves or {}

    async def query(self, expr, at=None):
        return []

    async def query_range(self, expr, start, end, step):
        out = []
        n = int((end - start) // 30)
        for node in ("sparky", "sparketa"):
            for (metric, who), (before, after) in self.moves.items():
                if who == node and HISTORY_QUERIES[metric].split("{")[0].split("(")[-1] in expr:
                    pts = [(start + i * 30, before + (i % 2) * 0.5 if i < n * 0.75 else after) for i in range(n + 1)]
                    out.append(Series(labels={"node": node}, points=pts))
                    break
            else:
                out.append(Series(labels={"node": node}, points=[(start + i * 30, 10.0 + (i % 2)) for i in range(n + 1)]))
        return out


def sources(prom, promql=False) -> agent_tools.Sources:
    async def nothing(*_):
        return None

    async def no_nodes():
        return []

    async def no_latency():
        return {"models": []}

    return agent_tools.Sources(prom=prom, nodes=no_nodes, latency=no_latency, clients=nothing,
                               promql_enabled=lambda: promql)


class TestCorrelate:
    def test_what_moved_is_ranked_and_marked_where_else_it_moved(self):
        at = time.time() - 3600
        prom = Prom({("swap_io", "sparky"): (0.0, 900.0), ("gpu_power", "sparky"): (10.0, 60.0),
                     ("gpu_power", "sparketa"): (10.0, 60.0)})
        r = asyncio.run(agent_tools.correlate(sources(prom), "sparky", agent_tools.iso(at), 10))
        moved = [x["metric"] for x in r["moved_on_node"]]
        assert moved[:2] == ["swap_io", "gpu_power"]
        power = next(x for x in r["moved_on_node"] if x["metric"] == "gpu_power")
        assert power["also_moved_on"] == ["sparketa"]
        assert all(x["score"] >= agent_tools.MOVED for x in r["moved_on_node"])
        # A series that stayed flat is not listed at all.
        assert "cpu_utilization" not in moved

    def test_its_window_is_stated(self):
        at = time.time() - 3600
        r = asyncio.run(agent_tools.correlate(sources(Prom()), "sparky", agent_tools.iso(at), 10))
        assert r["span"][0] == agent_tools.iso(at - 600)
        assert r["baseline"] == [agent_tools.iso(at - 2400), agent_tools.iso(at - 600)]
        assert r["moved_on_node"] == []


class TestPromql:
    def test_refused_while_switched_off(self):
        with pytest.raises(ToolError, match="switched off"):
            asyncio.run(agent_tools.promql(sources(Prom()), "up"))

    def test_hidden_from_the_list_while_off(self):
        server = agent_tools.build_server(sources(Prom(), promql=False))
        assert "promql" not in [t.name for t in asyncio.run(server.list_tools())]
        server = agent_tools.build_server(sources(Prom(), promql=True))
        assert "promql" in [t.name for t in asyncio.run(server.list_tools())]


class TestHistory:
    def test_points_are_on_the_step_grid_with_gaps_as_null(self):
        class P(Prom):
            async def query_range(self, expr, start, end, step):
                return [Series(labels={"node": "sparky"}, points=[(start, 1.0), (start + 30, 2.0)])]

        r = asyncio.run(agent_tools.metric_history(sources(P()), "gpu_power", minutes=2, points=True))
        assert r["step_s"] == 15
        values = r["series"][0]["values"]
        assert values[0] == 1.0 and values[1] is None and values[2] == 2.0

    def test_too_many_series_are_cut_and_say_so(self):
        class P(Prom):
            async def query_range(self, expr, start, end, step):
                return [Series(labels={"node": "n", "interface": f"if{i:02}"}, points=[(start, 1.0)])
                        for i in range(agent_tools.MAX_SERIES + 5)]

        r = asyncio.run(agent_tools.metric_history(sources(P()), "network_rx_bits"))
        assert len(r["series"]) == agent_tools.MAX_SERIES
        assert "pass nodes" in r["truncated"]


# --- Ask's loop over the tools (BB4) --------------------------------------------

CONFIG = assistant.AssistantConfig(url="http://gw.invalid/v1", model="m")


def scripted(monkeypatch, replies: list):
    """The endpoint answers each request with the next reply; records what
    it was sent."""
    sent: list[dict] = []

    async def fake_chat(url, key, body, timeout_s):
        sent.append(json.loads(json.dumps(body)))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(assistant, "_chat", fake_chat)
    return sent


def call_recorder():
    calls = []

    async def call(name, args):
        calls.append((name, args))
        return json.dumps({"ok": name}), False

    return calls, call


def tool_call(name, args, id_="c1"):
    return {"id": id_, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_the_model_calls_a_tool_then_answers(monkeypatch):
    sent = scripted(monkeypatch, [
        {"content": None, "tool_calls": [tool_call("correlate", {"node": "sparky", "at": "2026-10-16T15:44Z"})]},
        {"content": "Swap thrashing on sparky."},
    ])
    calls, call = call_recorder()
    r = asyncio.run(assistant.ask_with_tools(CONFIG, None, "why slow?", "ctx", [{"type": "function"}], call))
    assert r["answer"] == "Swap thrashing on sparky."
    assert calls == [("correlate", {"node": "sparky", "at": "2026-10-16T15:44Z"})]
    assert r["tools_used"][0]["tool"] == "correlate" and not r["tools_used"][0]["error"]
    # The second request carries the call and its result, in OpenAI's shape.
    assert sent[1]["messages"][-2]["tool_calls"][0]["id"] == "c1"
    assert sent[1]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": '{"ok": "correlate"}'}


def test_it_must_answer_after_the_last_round(monkeypatch):
    loop = [{"content": "", "tool_calls": [tool_call("cluster_now", {}, f"c{i}")]}
            for i in range(assistant.MAX_ROUNDS)]
    sent = scripted(monkeypatch, [*loop, {"content": "done"}])
    _, call = call_recorder()
    r = asyncio.run(assistant.ask_with_tools(CONFIG, None, "q", "ctx", [{"type": "function"}], call))
    assert r["answer"] == "done"
    assert len(r["tools_used"]) == assistant.MAX_ROUNDS
    assert "tools" not in sent[-1], "the final request offers no tools"


def test_bad_arguments_go_back_to_the_model_as_an_error(monkeypatch):
    scripted(monkeypatch, [
        {"tool_calls": [{"id": "x", "function": {"name": "alerts", "arguments": "{not json"}}]},
        {"content": "ok"},
    ])
    calls, call = call_recorder()
    r = asyncio.run(assistant.ask_with_tools(CONFIG, None, "q", "ctx", [], call))
    assert calls == [] and r["tools_used"][0]["error"] is True


def test_an_endpoint_that_refuses_tools_is_asked_without(monkeypatch):
    sent = scripted(monkeypatch, [
        assistant.EndpointRefused(400, "tools param requires --jinja flag"),
        {"content": "one-shot answer"},
    ])
    _, call = call_recorder()
    r = asyncio.run(assistant.ask_with_tools(CONFIG, None, "q", "ctx", [{"type": "function"}], call))
    assert r["answer"] == "one-shot answer" and "refused tools" in r["tools_note"]
    assert "tools" not in sent[-1]


def test_the_tools_switch_round_trips_through_the_cluster_file(tmp_path):
    cluster = tmp_path / "cluster.yml"
    cluster.write_text(NODES)
    assistant.write_assistant(cluster, assistant.AssistantConfig(url="http://gw:4000/v1", model="m", tools=False))
    assert yaml.safe_load(cluster.read_text())["assistant"]["tools"] is False
    assert assistant.load_assistant(cluster).tools is False
    assistant.write_assistant(cluster, assistant.AssistantConfig(url="http://gw:4000/v1", model="m"))
    assert "tools" not in yaml.safe_load(cluster.read_text())["assistant"]
    assert assistant.load_assistant(cluster).tools is True


def test_a_round_that_runs_out_of_time_still_ends_in_an_answer(monkeypatch):
    """The model's node busy with other work: the second round times out, and
    the time kept back answers from what the first round found."""
    import httpx

    sent = scripted(monkeypatch, [
        {"tool_calls": [tool_call("correlate", {"node": "sparky", "at": "2026-10-16T15:44Z"})]},
        httpx.ReadTimeout(""),
        {"content": "swap thrashing"},
    ])
    _, call = call_recorder()
    r = asyncio.run(assistant.ask_with_tools(CONFIG, None, "q", "ctx", [{"type": "function"}], call))
    assert r["answer"] == "swap thrashing" and r["tools_used"][0]["tool"] == "correlate"
    assert "tools" not in sent[-1]


def test_a_first_round_timeout_is_a_timeout(monkeypatch):
    import httpx

    scripted(monkeypatch, [httpx.ReadTimeout("")])
    _, call = call_recorder()
    with pytest.raises(httpx.TimeoutException):
        asyncio.run(assistant.ask_with_tools(CONFIG, None, "q", "ctx", [], call))


class TestStreamedAsk:
    """With tools on, the answer is streamed, so its status goes out before
    the outcome is known."""

    def setup(self, api, monkeypatch, replies):
        api.put("/api/assistant/config", json={"url": "http://gw.invalid:4000", "model": "m"})
        return scripted(monkeypatch, replies)

    def test_an_answer_arrives_as_json(self, api, monkeypatch):
        self.setup(api, monkeypatch, [
            {"tool_calls": [tool_call("list_metrics", {})]},
            {"content": "fine"},
        ])
        r = api.post("/api/assistant/ask", json={"question": "ok?", "minutes": 60})
        assert r.status_code == 200
        body = json.loads(r.text)
        assert body["answer"] == "fine"
        assert body["tools_used"] == [{"tool": "list_metrics", "args": {}, "error": False, "chars": body["tools_used"][0]["chars"]}]
        assert body["tools_used"][0]["chars"] > 100, "the real tool ran"

    def test_a_failure_is_named_in_the_body(self, api, monkeypatch):
        import httpx

        self.setup(api, monkeypatch, [httpx.ReadTimeout("")])
        r = api.post("/api/assistant/ask", json={"question": "ok?", "minutes": 60})
        body = json.loads(r.text)
        assert body["status"] == 504 and "did not answer within 180 s" in body["detail"]

    def test_with_tools_off_it_is_a_plain_answer(self, api, monkeypatch):
        api.put("/api/assistant/config", json={"url": "http://gw.invalid:4000", "model": "m", "tools": False})
        sent = scripted(monkeypatch, [{"content": "plain"}])
        r = api.post("/api/assistant/ask", json={"question": "ok?", "minutes": 60})
        assert r.status_code == 200 and r.json()["answer"] == "plain"
        assert "tools" not in sent[0]


def test_a_flat_zero_that_twitches_does_not_head_the_ranking():
    """Memory pressure 0.000 -> 0.015% scored 246 and ranked above ComfyUI
    taking the GPU from 0 to 58% (2026-10-16). Each unit has a least change
    that counts."""
    at = time.time() - 3600

    class P(Prom):
        async def query_range(self, expr, start, end, step):
            n = int((end - start) // 30)
            cut = n * 0.75
            if "psi_memory_some" in expr:
                return [Series(labels={"node": "sparky"}, points=[(start + i * 30, 0.0 if i < cut else 0.015) for i in range(n + 1)])]
            if "gpu_utilization" in expr:
                return [Series(labels={"node": "sparky"}, points=[(start + i * 30, 0.0 if i < cut else 58.0) for i in range(n + 1)])]
            return []

    r = asyncio.run(agent_tools.correlate(sources(P()), "sparky", agent_tools.iso(at), 10))
    moved = [x["metric"] for x in r["moved_on_node"]]
    assert moved[0] == "gpu_utilization"
    assert "psi_some_avg10" not in moved


def test_every_unit_has_a_floor():
    units = {u for u, _ in agent_tools.METRICS.values()}
    assert units <= set(agent_tools.UNIT_FLOOR), sorted(units - set(agent_tools.UNIT_FLOOR))



# --- BB8-BB14 ----------------------------------------------------------------

def test_monitoring_health_finds_a_gap_and_a_target_down_now():
    now = time.time()

    class P(Prom):
        async def query_range(self, expr, start, end, step):
            st = int(step.rstrip("s"))
            n = int((end - start) // st)
            # sparky's agent: missing for 10 steps in the middle, then back.
            pts = [(start + i * st, 1.0) for i in range(n + 1) if not (20 <= i < 30)]
            # sparkjr's: down at the end.
            down = [(start + i * st, 1.0 if i < n - 5 else 0.0) for i in range(n + 1)]
            return [Series(labels={"job": "spark-dash-agent", "instance": "a:9500", "node": "sparky"}, points=pts),
                    Series(labels={"job": "spark-dash-agent", "instance": "c:9500", "node": "sparkjr"}, points=down)]

        async def data_age_s(self):
            return 2.0

    r = asyncio.run(agent_tools.monitoring_health(sources(P()), minutes=60, end=agent_tools.iso(now)))
    by = {t["node"]: t for t in r["targets_with_gaps_or_down"]}
    assert len(by["sparky"]["gaps"]) == 1 and by["sparky"]["up_now"] is True
    assert by["sparkjr"]["up_now"] is False
    assert r["prometheus_newest_sample_age_s"] == 2.0


def test_compare_ranks_what_moved_between_two_windows():
    a_end, b_end = time.time() - 86400, time.time()

    class P(Prom):
        async def query_range(self, expr, start, end, step):
            st = int(step.rstrip("s"))
            hot = start > a_end  # window B
            n = int((end - start) // st)
            if "gpu_power" in expr:
                return [Series(labels={"node": "sparky"}, points=[(start + i * st, (60.0 if hot else 10.0) + i % 2) for i in range(n + 1)])]
            return [Series(labels={"node": "sparky"}, points=[(start + i * st, 10.0 + i % 2) for i in range(n + 1)])]

    r = asyncio.run(agent_tools.compare(sources(P()), agent_tools.iso(a_end), agent_tools.iso(b_end), 60))
    assert [m["metric"] for m in r["moved"]] == ["gpu_power"]
    assert r["moved"][0]["a_avg"] < 11 and r["moved"][0]["b_avg"] > 59


def test_topology_groups_pooled_nodes():
    from spark_dash_backend.cluster import ClusterNode
    src = sources(Prom())
    src.cluster_nodes = lambda: [ClusterNode("sparketa", "10.0.0.2", cluster="pool"),
                                 ClusterNode("sparkjr", "10.0.0.3", cluster="pool"),
                                 ClusterNode("sparky", "10.0.0.1")]
    r = asyncio.run(agent_tools.topology(src))
    assert r["clusters"] == [{"name": "pool", "nodes": ["sparketa", "sparkjr"]}]
    assert [n["node"] for n in r["nodes"]] == ["sparketa", "sparkjr", "sparky"]


def test_alerting_lists_rules_and_says_when_alertmanager_is_down():
    class P(Prom):
        async def rules(self):
            return [{"type": "alerting", "name": "GpuHot", "group": "thermal", "query": "x > 1",
                     "duration": 300, "labels": {"severity": "warning"}, "state": "firing",
                     "alerts": [{"labels": {"node": "sparky"}, "state": "firing", "activeAt": "t"}]},
                    {"type": "recording", "name": "r"}]

    async def down():
        raise RuntimeError("no")

    src = sources(P())
    src.silences = down
    r = asyncio.run(agent_tools.alerting(src))
    assert [x["alert"] for x in r["rules"]] == ["GpuHot"]
    assert r["firing_or_pending"] == ["GpuHot"]
    assert r["rules"][0]["active"] == [{"node": "sparky", "state": "firing", "since": "t"}]
    assert r["silences"] == "Alertmanager did not answer"


def test_updates_summarises_the_fleet_and_says_when_it_is_off():
    assert asyncio.run(agent_tools.updates(sources(Prom())))["configured"] is False

    async def fleet():
        return {"last_sweep": "t", "interval_min": 60, "nodes": [{
            "name": "sparky", "reachable": True, "kernel": "7.0",
            "software": {"state": "installable", "behind": ["a", "b"]},
            "firmware_gap": {"state": "current", "outstanding": []},
            "release": {"on": "OTA2607", "latest": "OTA2607"}}]}

    src = sources(Prom())
    src.fleet = fleet
    r = asyncio.run(agent_tools.updates(src))
    assert r["nodes"][0]["software"]["behind"] == 2
    assert r["nodes"][0]["release"]["on"] == "OTA2607"


def test_latency_over_a_window_names_the_peak():
    async def over(start, end, step):
        return {"window_each_step": "300s", "models": [{
            "model": "GLM", "node": "sparketa", "runtime": "tensorfold", "source": "engine", "requests": 446,
            "p50": {"avg_s": 1.4, "max_s": 6.2, "max_at": start + 60},
            "p95": {"avg_s": 21.7, "max_s": 300.0, "max_at": start + 120},
            "series": {"p50": [(start, 1.0)], "p95": [(start, 2.0)]}}]}

    src = sources(Prom())
    src.latency_over = over
    r = asyncio.run(agent_tools.latency(src, minutes=60, points=True))
    m = r["models"][0]
    assert m["p95"]["max_s"] == 300.0 and m["p95"]["max_at"].endswith("Z")
    assert m["p50_values_s"][0] == 1.0 and "series" not in m


# --- BB15, BB16 ----------------------------------------------------------------

def test_the_guides_and_prompts_are_served(api):
    switch_on(api)
    h = api.headers_with_key
    res = api.post("/mcp", json=rpc("resources/list"), headers=h).json()["result"]["resources"]
    uris = sorted(r["uri"] for r in res)
    assert uris == ["sparkdash://guide/gb10", "sparkdash://guide/investigating", "sparkdash://guide/metrics"]
    body = api.post("/mcp", json=rpc("resources/read", {"uri": "sparkdash://guide/metrics"}), headers=h).json()
    text = body["result"]["contents"][0]["text"]
    for key in HISTORY_QUERIES:
        assert f"## {key} (" in text, f"{key} is missing from the metrics guide"
    prompts = api.post("/mcp", json=rpc("prompts/list"), headers=h).json()["result"]["prompts"]
    assert sorted(p["name"] for p in prompts) == ["capacity_check", "daily_report", "incident_report"]
    got = api.post("/mcp", json=rpc("prompts/get", {"name": "incident_report", "arguments": {
        "node": "sparky", "start": "2026-10-15T12:00Z", "end": "2026-10-15T16:00Z"}}), headers=h).json()
    msg = got["result"]["messages"][0]["content"]["text"]
    assert "sparky" in msg and "correlate" in msg and "monitoring_health" in msg


def test_the_gb10_guide_states_the_facts_the_dashboard_rests_on():
    from spark_dash_backend.agent_docs import GB10
    for fact in ("one pool", "NVML", "MemTotal - MemAvailable", "104.8", "3003 MHz", "temperature_bands_c"):
        assert fact in GB10, fact



# --- BB17: notes ----------------------------------------------------------------

class TestNotes:
    def call(self, api, name, args):
        return api.post("/mcp", headers=api.headers_with_key, json=rpc("tools/call", {
            "name": name, "arguments": args})).json()["result"]

    def names(self, api):
        return [t["name"] for t in api.post("/mcp", json=rpc("tools/list"), headers=api.headers_with_key).json()["result"]["tools"]]

    def test_off_by_default_hidden_and_refused(self, api):
        switch_on(api)
        assert "add_note" not in self.names(api)
        r = self.call(api, "add_note", {"text": "benchmark started"})
        assert r["isError"] and "switched off" in r["content"][0]["text"]

    def test_an_agent_adds_one_and_the_timeline_shows_it(self, api):
        switch_on(api, notes=True)
        tools = {t["name"]: t for t in api.post("/mcp", json=rpc("tools/list"), headers=api.headers_with_key).json()["result"]["tools"]}
        assert tools["add_note"]["annotations"]["readOnlyHint"] is False
        r = self.call(api, "add_note", {"text": "benchmark  started", "node": "sparky"})
        added = json.loads(r["content"][0]["text"])["added"]
        assert added["text"] == "benchmark started" and added["by"] == "agent"
        ann = api.get("/api/annotations", params={"minutes": 5}).json()["annotations"]
        assert any(a["kind"] == "note" and a["label"] == "benchmark started (agent)" and a["node"] == "sparky"
                   for a in ann)
        ev = json.loads(self.call(api, "events", {"minutes": 5})["content"][0]["text"])["events"]
        assert [e["kind"] for e in ev] == ["note"]
        status = api.get("/api/mcp/status").json()
        assert status["notes"] is True and status["note_count"] == 1
        assert api.delete("/api/notes").json()["removed"] == 1
        assert api.get("/api/mcp/status").json()["note_count"] == 0

    def test_a_note_is_bounded(self, api):
        switch_on(api, notes=True)
        r = self.call(api, "add_note", {"text": "x" * 201})
        assert r["isError"] and "200" in r["content"][0]["text"]
        assert self.call(api, "add_note", {"text": "   "})["isError"]
