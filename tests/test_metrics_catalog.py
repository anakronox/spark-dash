"""The metric catalogue in docs/metrics.md matches what the agent exports.

Documentation drifts silently and this is the kind that costs most: someone
builds a Grafana panel on a name that no longer exists and gets an empty chart,
which is indistinguishable from a quiet cluster. It was also the gap that made
"can I build my own dashboard?" answer badly -- the data was always there, the
description of it was not.

Checked in BOTH directions. A metric added without a doc entry is undocumented;
a doc entry with no metric is a name someone will query and get nothing back.

Names are obtained by RENDERING a snapshot rather than grepping the exporter:
metric names are built there by three different patterns -- `_g("name", ...)`,
lists of `(name, doc, value)` tuples, and f-strings per engine -- so a regex
over the source silently misses whichever one it was not written for.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

DOC = Path(__file__).resolve().parent.parent / "docs" / "metrics.md"

#: Documented as a family shorthand rather than one row per engine, because the
#: rows would be identical. Expanded here to compare against what is exported.
ENGINE_TEMPLATES = (
    "{engine}_generation_tokens_per_second",
    "{engine}_prompt_tokens_per_second",
    "{engine}_tokens_per_second",
    "{engine}_requests_running",
    "{engine}_requests_waiting",
    "{engine}_kv_cache_percent",
)


def exported() -> set[str]:
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
        node_id="n1", ts=datetime.now(UTC), up=True, cluster="c1",
        health=HealthState.GOOD, unmonitored_runtimes=["vllm"],
        temp_bands=TempBands(gpu_warning_c=82.0, gpu_critical_c=86.0,
                             gpu_source="nvml-slowdown", cpu_warning_c=92.8,
                             cpu_critical_c=98.8, cpu_source="acpi-critical-trip"),
        gpu=GpuMetrics(util_pct=50.0, temp_c=60.0, power_w=90.0, clock_mhz=2400.0,
                       clock_state=ClockState.PASS, target_clock_mhz=2418.0),
        memory=MemoryMetrics(total_bytes=1, available_bytes=1, used_bytes=1,
                             swap_used_bytes=0, unified=True),
        disk=DiskMetrics(total_bytes=1, available_bytes=1, used_bytes=1),
        psi=PsiMetrics(some_avg10=1.0, some_avg60=1.0, full_avg10=0.0,
                       full_avg60=0.0, state=PsiState.LOW),
        cpu=CpuMetrics(util_pct=20.0, load_avg_1m=1.0, temp_c=55.0),
        processes=[ProcessInfo(pid=1, name="x", gpu_mem_bytes=1, runtime="llama.cpp",
                               model="m", server="h:8001")],
        network=[NetworkInterface(name="eth0", up=True, speed_mbps=10000, monitored=True)],
        rdma=[RdmaPort(device="roce0", port=1, state="ACTIVE", interface="eth0",
                       link_layer="Ethernet", rate="200 Gb/sec", monitored=True)],
        runtimes=Runtimes(
            llama_cpp=[LlamaRouterMetrics(
                endpoint="http://h:8001", name="h:8001", max_instances=3,
                models=[RouterModel(name="m", state=ModelState.ACTIVE, size_bytes=1,
                                    n_params=1, context_length=1, tokens_per_sec=1.0,
                                    generation_tokens_per_sec=1.0,
                                    prompt_tokens_per_sec=1.0, kv_cache_pct=1.0)])],
            vllm=[EngineMetrics(model="v", server="h:8120", kv_cache_pct=1.0)],
            sglang=[EngineMetrics(model="s", server="h:30000")]),
        errors={"psi": "OSError"},
    )
    registry = CollectorRegistry()
    registry.register(SnapshotMetricsCollector(lambda: snap))
    registry.register(CollectionStatsCollector(
        lambda: CacheStats(collect_duration_s=0.4, snapshot_age_s=1.0,
                           collections=10, failures=0, stalled=False), "n1"))
    text = generate_latest(registry).decode()
    return set(re.findall(r"^# HELP (sparkdash_[a-z0-9_]+)", text, re.M))


def documented() -> set[str]:
    """Names from the FIRST CELL of each catalogue table row.

    Parsed that narrowly on purpose: the prose around the tables mentions label
    names, PromQL fragments and engine metrics in backticks too, and a looser
    match pulls all of it in. The first cell is the one place a row commits to
    naming a metric.

    Two shorthands are expanded rather than written out: `{engine}_x` for the
    per-engine families, and `a_{x,y}_b` for the receive/transmit pairs.
    """
    from spark_dash_common.models import ENGINE_RUNTIMES

    body = DOC.read_text()
    body = body[body.index("## What the agent itself exports"):]

    names: set[str] = set()
    for line in body.splitlines():
        if not line.startswith("|"):
            continue
        cell = line.split("|")[1].strip()
        if not cell or set(cell) <= {"-", ":", " "} or cell == "metric":
            continue
        for raw in re.findall(r"`([^`]+)`", cell):
            if "{engine}" in raw:
                names.update(raw.replace("{engine}", r) for r in ENGINE_RUNTIMES)
            elif (m := re.fullmatch(r"([a-z0-9_]+)\{([a-z,]+)\}([a-z0-9_]*)", raw)):
                stem, alts, tail = m.groups()
                names.update(f"{stem}{a}{tail}" for a in alts.split(","))
            elif re.fullmatch(r"[a-z0-9_]+", raw):
                names.add(raw)
    return {f"sparkdash_{n}" for n in names}


@pytest.fixture(scope="module")
def sets() -> tuple[set[str], set[str]]:
    return exported(), documented()


def test_every_exported_metric_is_documented(sets):
    exp, doc = sets
    missing = sorted(n.replace("sparkdash_", "") for n in exp - doc)
    assert not missing, f"exported but absent from docs/metrics.md: {missing}"


def test_every_documented_metric_exists(sets):
    """A name in the docs that the agent does not emit is one someone will
    query and get nothing back from."""
    exp, doc = sets
    phantom = sorted(n.replace("sparkdash_", "") for n in doc - exp)
    assert not phantom, f"documented but never exported: {phantom}"
