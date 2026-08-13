"""Renders a `NodeSnapshot` as Prometheus metrics.

Deliberately omits the per-process list. PIDs churn constantly, and a `pid`
label would grow Prometheus's series cardinality without bound for data nobody
queries historically. The process view is a live-view concern and is served as
JSON from `/snapshot` instead.
"""

from __future__ import annotations

from collections.abc import Iterable

from prometheus_client.core import GaugeMetricFamily
from spark_dash_common.models import ClockState, HealthState, NodeSnapshot, PsiState

_NS = "sparkdash"


class SnapshotMetricsCollector:
    """prometheus_client custom collector backed by the snapshot cache."""

    def __init__(self, get_snapshot) -> None:
        self._get_snapshot = get_snapshot

    def collect(self) -> Iterable[GaugeMetricFamily]:
        snap: NodeSnapshot = self._get_snapshot()
        node = snap.node_id
        yield from _node_metrics(snap, node)
        yield from _gpu_metrics(snap, node)
        yield from _memory_metrics(snap, node)
        yield from _psi_metrics(snap, node)
        yield from _cpu_metrics(snap, node)
        yield from _runtime_metrics(snap, node)


def _g(name: str, doc: str, labels: list[str] | None = None) -> GaugeMetricFamily:
    return GaugeMetricFamily(f"{_NS}_{name}", doc, labels=labels or [])


def _node_metrics(snap: NodeSnapshot, node: str) -> Iterable[GaugeMetricFamily]:
    up = _g("node_up", "1 if the agent produced a snapshot", ["node"])
    up.add_metric([node], 1.0 if snap.up else 0.0)
    yield up

    # One series per state with a 0/1 value, so alerting rules can match on the
    # state label rather than decoding an integer enum.
    health = _g("node_health", "Node health state (1 for the active state)", ["node", "state"])
    for state in HealthState:
        health.add_metric([node, state.value], 1.0 if snap.health is state else 0.0)
    yield health

    errors = _g("collector_errors", "1 when a collector failed this scrape", ["node", "collector"])
    for collector, _msg in snap.errors.items():
        errors.add_metric([node, collector], 1.0)
    yield errors


def _gpu_metrics(snap: NodeSnapshot, node: str) -> Iterable[GaugeMetricFamily]:
    gpu = snap.gpu
    if gpu is None:
        return

    util = _g("gpu_utilization_percent", "GPU utilization", ["node"])
    util.add_metric([node], gpu.util_pct)
    yield util

    if gpu.temp_c is not None:
        temp = _g("gpu_temperature_celsius", "GPU temperature", ["node"])
        temp.add_metric([node], gpu.temp_c)
        yield temp

    if gpu.power_w is not None:
        power = _g("gpu_power_watts", "GPU board power draw", ["node"])
        power.add_metric([node], gpu.power_w)
        yield power

    if gpu.clock_mhz is not None:
        clock = _g("gpu_clock_mhz", "GPU SM clock frequency", ["node"])
        clock.add_metric([node], gpu.clock_mhz)
        yield clock

    state = _g(
        "gpu_clock_state",
        "GPU clock state, load-gated (1 for the active state)",
        ["node", "state"],
    )
    for value in ClockState:
        state.add_metric([node, value.value], 1.0 if gpu.clock_state is value else 0.0)
    yield state


def _memory_metrics(snap: NodeSnapshot, node: str) -> Iterable[GaugeMetricFamily]:
    mem = snap.memory
    if mem is None:
        return

    for name, doc, value in (
        ("memory_total_bytes", "Total system memory", mem.total_bytes),
        ("memory_available_bytes", "Memory available without swapping", mem.available_bytes),
        ("memory_used_bytes", "Memory in use (total - available)", mem.used_bytes),
        ("memory_swap_used_bytes", "Swap in use", mem.swap_used_bytes),
    ):
        metric = _g(name, doc, ["node"])
        metric.add_metric([node], float(value))
        yield metric

    # Flags that memory_* describes one coherent CPU+GPU pool (GB10) rather
    # than system RAM alongside separate VRAM.
    unified = _g("memory_unified", "1 when CPU and GPU share one coherent pool", ["node"])
    unified.add_metric([node], 1.0 if mem.unified else 0.0)
    yield unified


def _psi_metrics(snap: NodeSnapshot, node: str) -> Iterable[GaugeMetricFamily]:
    psi = snap.psi
    if psi is None:
        return

    for name, doc, value in (
        ("psi_memory_some_avg10", "Time at least one task stalled, 10s (%)", psi.some_avg10),
        ("psi_memory_some_avg60", "Time at least one task stalled, 60s (%)", psi.some_avg60),
        ("psi_memory_full_avg10", "Time all tasks stalled, 10s (%)", psi.full_avg10),
        ("psi_memory_full_avg60", "Time all tasks stalled, 60s (%)", psi.full_avg60),
    ):
        metric = _g(name, doc, ["node"])
        metric.add_metric([node], value)
        yield metric

    state = _g("psi_memory_state", "Memory pressure band (1 for active)", ["node", "state"])
    for value_state in PsiState:
        state.add_metric([node, value_state.value], 1.0 if psi.state is value_state else 0.0)
    yield state


def _cpu_metrics(snap: NodeSnapshot, node: str) -> Iterable[GaugeMetricFamily]:
    cpu = snap.cpu
    if cpu is None:
        return

    util = _g("cpu_utilization_percent", "CPU utilization", ["node"])
    util.add_metric([node], cpu.util_pct)
    yield util

    if cpu.temp_c is not None:
        temp = _g("cpu_temperature_celsius", "CPU temperature", ["node"])
        temp.add_metric([node], cpu.temp_c)
        yield temp

    if cpu.load_avg_1m is not None:
        load = _g("cpu_load1", "1-minute load average", ["node"])
        load.add_metric([node], cpu.load_avg_1m)
        yield load


def _runtime_metrics(snap: NodeSnapshot, node: str) -> Iterable[GaugeMetricFamily]:
    if snap.runtimes.llama_cpp:
        # Every series carries a `router` label: a node runs several router
        # containers, and the same model name can be registered with more than
        # one of them.
        rl = ["node", "router"]
        rml = ["node", "router", "model"]

        up = _g("llama_router_up", "1 when the router answered", rl)
        known = _g("llama_models_known", "Models registered with the router", rl)
        loaded_count = _g("llama_models_loaded", "Models currently resident", rl)
        loaded = _g("llama_model_loaded", "1 while a model is resident", rml)
        tps = _g("llama_model_tokens_per_second", "Token throughput", rml)
        kv = _g("llama_model_kv_cache_percent", "KV cache utilization", rml)
        running = _g("llama_model_requests_running", "In-flight requests", rml)
        waiting = _g("llama_model_requests_waiting", "Queued requests", rml)

        for router in snap.runtimes.llama_cpp:
            label = router.name or router.endpoint
            up.add_metric([node, label], 1.0 if router.reachable else 0.0)
            known.add_metric([node, label], float(router.known_model_count))
            loaded_count.add_metric([node, label], float(len(router.loaded_models)))

            for model in router.loaded_models:
                loaded.add_metric([node, label, model.name], 1.0)
                tps.add_metric([node, label, model.name], model.tokens_per_sec or 0.0)
                if model.kv_cache_pct is not None:
                    kv.add_metric([node, label, model.name], model.kv_cache_pct)
                running.add_metric([node, label, model.name], float(model.requests_running))
                waiting.add_metric([node, label, model.name], float(model.requests_waiting))

        yield from (up, known, loaded_count, loaded, tps, kv, running, waiting)

    if snap.runtimes.vllm:
        tps = _g("vllm_tokens_per_second", "Token throughput", ["node", "model"])
        kv = _g("vllm_kv_cache_percent", "KV cache utilization", ["node", "model"])
        running = _g("vllm_requests_running", "In-flight requests", ["node", "model"])
        waiting = _g("vllm_requests_waiting", "Queued requests", ["node", "model"])

        for instance in snap.runtimes.vllm:
            tps.add_metric([node, instance.model], instance.tokens_per_sec)
            if instance.kv_cache_pct is not None:
                kv.add_metric([node, instance.model], instance.kv_cache_pct)
            running.add_metric([node, instance.model], float(instance.requests_running))
            waiting.add_metric([node, instance.model], float(instance.requests_waiting))

        yield from (tps, kv, running, waiting)
