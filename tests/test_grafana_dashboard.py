"""The starter Grafana dashboard stays valid and stays honest.

A dashboard JSON is the easiest artifact in this repo to let rot: nothing
imports it, no test exercises it, and a metric renamed three commits later
leaves a panel that renders an empty chart rather than an error. These checks
are cheap and catch the two ways it goes wrong — a query naming a series the
agent no longer exports, and the aggregation mistakes this deployment has
already made once.

What is NOT checked here: Grafana's own rendering, and the transformations in
the multi-query tables. Both need Grafana. The README says so rather than
implying the file was exercised end to end.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "central" / "grafana" / "spark-dash-overview.json"
EXPORTER = ROOT / "agent" / "src" / "spark_dash_agent" / "exporter.py"


@pytest.fixture(scope="module")
def dashboard() -> dict:
    return json.loads(DASHBOARD.read_text())


def panels(node: dict) -> list[dict]:
    """Every panel, including those nested inside a collapsed row."""
    out = []
    for p in node.get("panels", []):
        out.append(p)
        out.extend(panels(p))
    return out


def exprs(dashboard: dict) -> list[tuple[str, str]]:
    return [
        (p.get("title", "?"), t["expr"])
        for p in panels(dashboard)
        for t in p.get("targets", [])
        if "expr" in t
    ]


def exported_metric_names() -> set[str]:
    """Series the agent actually emits, obtained by RENDERING one.

    Rendering rather than grepping the exporter: metric names are built there by
    three different patterns — `_g("name", ...)`, a list of `(name, doc, value)`
    tuples, and f-strings per engine — so a regex over the source silently
    misses whichever one it was not written for. Asking the exporter is the only
    version of this check that cannot quietly under-report.

    The snapshot below is deliberately fully populated: every section the
    exporter can skip when empty has to be present, or this returns a subset and
    the assertion it feeds stops meaning anything.
    """
    from datetime import UTC, datetime

    from prometheus_client import CollectorRegistry, generate_latest
    from spark_dash_agent.app import CacheStats
    from spark_dash_agent.exporter import CollectionStatsCollector, SnapshotMetricsCollector
    from spark_dash_common.models import (
        ClockState,
        CpuMetrics,
        DiskMetrics,
        EngineMetrics,
        GpuMetrics,
        HealthState,
        LlamaRouterMetrics,
        MemoryMetrics,
        ModelState,
        NetworkInterface,
        NodeSnapshot,
        ProcessInfo,
        PsiMetrics,
        PsiState,
        RdmaPort,
        RouterModel,
        Runtimes,
        TempBands,
    )

    snap = NodeSnapshot(
        node_id="n1",
        ts=datetime.now(UTC),
        up=True,
        health=HealthState.GOOD,
        unmonitored_runtimes=["vllm"],
        temp_bands=TempBands(
            gpu_warning_c=82.0, gpu_critical_c=86.0, gpu_source="nvml-slowdown",
            cpu_warning_c=92.8, cpu_critical_c=98.8, cpu_source="thermal-zone",
        ),
        gpu=GpuMetrics(
            util_pct=50.0, temp_c=60.0, power_w=90.0,
            clock_mhz=2400.0, clock_state=ClockState.PASS, target_clock_mhz=2418.0,
        ),
        memory=MemoryMetrics(
            total_bytes=128 * 2**30, available_bytes=64 * 2**30,
            used_bytes=64 * 2**30, swap_used_bytes=0, unified=True,
        ),
        disk=DiskMetrics(total_bytes=4 * 2**40, available_bytes=2 * 2**40,
                         used_bytes=2 * 2**40),
        psi=PsiMetrics(
            some_avg10=1.0, some_avg60=1.0, full_avg10=0.0, full_avg60=0.0,
            state=PsiState.LOW,
        ),
        cpu=CpuMetrics(util_pct=20.0, load_avg_1m=1.0, temp_c=55.0),
        processes=[ProcessInfo(pid=1, name="llama-server", gpu_mem_bytes=2**30,
                               runtime="llama.cpp", model="m", server="h:8001")],
        network=[NetworkInterface(name="eth0", up=True, speed_mbps=10000, monitored=True)],
        rdma=[RdmaPort(device="roce0", port=1, state="ACTIVE", interface="eth0",
                       link_layer="Ethernet", rate="200 Gb/sec", monitored=True)],
        runtimes=Runtimes(
            llama_cpp=[LlamaRouterMetrics(
                endpoint="http://h:8001", name="h:8001", max_instances=3,
                models=[RouterModel(name="m", state=ModelState.ACTIVE,
                                    size_bytes=1, n_params=1, context_length=1,
                                    tokens_per_sec=1.0, kv_cache_pct=1.0)],
            )],
            vllm=[EngineMetrics(model="v", server="h:8120", kv_cache_pct=1.0)],
            sglang=[EngineMetrics(model="s", server="h:30000")],
        ),
        # A failed collector, because `collector_errors` emits a series per
        # entry and an empty dict yields the family with no samples at all.
        errors={"psi": "OSError: nope"},
    )
    registry = CollectorRegistry()
    registry.register(SnapshotMetricsCollector(lambda: snap))
    # The agent-health series come from a SECOND collector describing the agent
    # itself rather than the node, and the dashboard's collapsed row uses them.
    registry.register(CollectionStatsCollector(
        lambda: CacheStats(collect_duration_s=0.4, snapshot_age_s=1.0,
                           collections=10, failures=0, stalled=False),
        "n1",
    ))
    text = generate_latest(registry).decode()
    return set(re.findall(r"^(sparkdash_[a-z0-9_]+)(?:\{|\s)", text, re.M))


def test_dashboard_is_valid_json_with_unique_panel_ids(dashboard):
    ids = [p["id"] for p in panels(dashboard)]
    assert len(ids) == len(set(ids)), "duplicate panel ids confuse Grafana's layout"


def test_it_imports_without_a_hardcoded_datasource(dashboard):
    """Declared as an __inputs entry, so it lands in any Grafana with a picker
    rather than silently pointing at a uid that does not exist there."""
    assert dashboard["__inputs"][0]["name"] == "DS_PROMETHEUS"
    blob = json.dumps(dashboard)
    uids = set(re.findall(r'"uid":\s*"([^"]+)"', blob))
    assert uids <= {"${DS_PROMETHEUS}", "-- Grafana --", dashboard["uid"]}, (
        f"a datasource uid is hardcoded: {uids}"
    )


def test_every_panel_explains_itself(dashboard):
    """The descriptions are the catalog for this metric surface until X3 lands,
    so a panel without one is a gap in the docs, not just a terse UI."""
    missing = [p["title"] for p in panels(dashboard)
               if p["type"] != "row" and not p.get("description")]
    assert not missing, f"panels with no description: {missing}"


def test_every_sparkdash_series_referenced_is_one_the_agent_exports(dashboard):
    """The rot this file exists to catch. A renamed metric leaves a panel that
    renders empty rather than failing, which is indistinguishable from a quiet
    cluster."""
    exported = exported_metric_names()
    referenced = set()
    for _, expr in exprs(dashboard):
        referenced |= set(re.findall(r"\bsparkdash_[a-z0-9_]+", expr))
    unknown = sorted(referenced - exported)
    assert not unknown, f"dashboard queries name series the agent does not export: {unknown}"


def test_name_regexes_expand_to_series_that_exist(dashboard):
    """`{__name__=~"sparkdash_(a|b)_x"}` names no metric literally, so the
    previous check cannot see inside it — and that is where engine names are
    hardcoded. Expand the alternation and check each branch."""
    exported = exported_metric_names()
    pattern = re.compile(r'__name__=~"([^"]+)"')
    checked = 0
    for title, expr in exprs(dashboard):
        for raw in pattern.findall(expr):
            m = re.fullmatch(r"([a-z0-9_]*)\(([a-z0-9_|]+)\)([a-z0-9_]*)", raw)
            assert m, f"{title}: cannot expand {raw!r}; keep these regexes simple enough to verify"
            head, alts, tail = m.groups()
            for alt in alts.split("|"):
                name = f"{head}{alt}{tail}"
                assert name in exported, f"{title}: {name} is not exported by the agent"
                checked += 1
    assert checked, "no __name__ regexes found — has the throughput query changed shape?"


def test_every_engine_appears_in_the_throughput_regex(dashboard):
    """ENGINE_RUNTIMES is the source of truth for which engines exist. An engine
    added there but not here charts as missing throughput on any node running
    it — which reads as an idle node, not as a broken panel."""
    from spark_dash_common.models import ENGINE_RUNTIMES

    tps = [e for _, e in exprs(dashboard) if "tokens_per_second" in e]
    assert tps, "the throughput queries have gone missing"
    for runtime in ENGINE_RUNTIMES:
        assert any(runtime in e for e in tps), (
            f"{runtime} is an engine but appears in no throughput query"
        )


def test_throughput_is_one_sum_over_a_regex_not_a_sum_per_engine(dashboard):
    """`sum(A) + sum(B)` keeps only label sets present on BOTH sides, so a node
    running one engine charts flat zero while it serves tokens. That was a live
    bug in HISTORY_QUERIES; the dashboard must not reintroduce it."""
    for title, expr in exprs(dashboard):
        if "tokens_per_second" not in expr:
            continue
        assert "__name__=~" in expr, f"{title}: select the families by name regex"
        assert not re.search(r"\)\s*\+\s*sum", expr), (
            f"{title}: summing engines with `+` drops nodes running only one of them"
        )


def test_the_memory_pool_panel_is_per_node(dashboard):
    """GB10 has no separate VRAM. Summing pools across nodes describes one big
    space that nobody can allocate from, which is the same arithmetic error the
    `cluster` label exists to prevent."""
    pool = [p for p in panels(dashboard) if "eating the pool" in p.get("title", "")]
    assert pool, "the pool-composition panel has gone missing"
    panel = pool[0]
    assert panel.get("repeat") == "node", "the pool panel must repeat per node"
    for t in panel["targets"]:
        assert 'node="$node"' in t["expr"], (
            "a repeated panel pins one node; `node=~\"$node\"` would re-aggregate all of them"
        )


def test_counter_series_are_rated_not_plotted_raw(dashboard):
    """The network byte counters are monotonic since boot. Plotting them raw
    draws a ramp that says nothing; they are also TYPED as gauges, so nothing
    in Grafana warns about it."""
    for title, expr in exprs(dashboard):
        for series in re.findall(r"sparkdash_network_(?:receive|transmit)_bytes_total", expr):
            assert f"rate({series}" in expr.replace(" ", ""), (
                f"{title}: {series} needs rate()"
            )
