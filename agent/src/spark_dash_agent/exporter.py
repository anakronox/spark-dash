"""Renders a `NodeSnapshot` as Prometheus metrics.

The per-process list is exported **aggregated by workload identity, never by
pid**. A `pid` label would grow cardinality without bound — pids churn on every
model swap and the old series would never be reused — so the raw process list
stays a live-view concern, served as JSON from `/snapshot`.

Grouping by `(runtime, model, router)` keeps cardinality bounded by
configuration rather than by uptime, and makes the interesting question
answerable historically: on GB10 every GPU workload competes for one unified
pool, so "what was holding the pool at 3am" is a capacity question, not a
curiosity. The `model` label is the same string the router reports, so these
series join directly to `sparkdash_llama_model_*`.
"""

from __future__ import annotations

from collections.abc import Iterable

from prometheus_client.core import GaugeMetricFamily
from spark_dash_common.models import (
    ClockState,
    HealthState,
    ModelState,
    NodeSnapshot,
    PsiState,
)

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
        yield from _network_metrics(snap, node)
        yield from _process_metrics(snap, node)
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

    # An info-style metric, same shape as rdma_port_info: a git sha can't be a
    # gauge value, so it rides as a label and the value is a constant 1.
    #
    # Load-bearing rather than cosmetic. Images track :latest, so no config
    # records which build a node is on — this is the only historical answer to
    # "what was running when", and a more honest one than a pinned tag, since it
    # reports what actually ran rather than what was intended. It also lets any
    # metric be stamped with its build:
    #
    #   sparkdash_gpu_utilization_percent
    #     * on(node) group_left(build) sparkdash_agent_build_info
    #
    # which is what separates "the GPU numbers changed" from "the agent that
    # measures them changed" — a distinction that has cost real debugging time.
    #
    # `build` churns by design: each new build starts a new series and the old
    # one goes stale. That's bounded by how many builds get deployed, not by
    # uptime, so it stays small.
    build = _g(
        "agent_build_info",
        "Always 1; carries the agent's build sha as a label",
        ["node", "build"],
    )
    build.add_metric([node, snap.agent_version], 1.0)
    yield build


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


def _network_metrics(snap: NodeSnapshot, node: str) -> Iterable[GaugeMetricFamily]:
    if snap.network:
        nl = ["node", "interface"]
        up = _g("network_up", "1 when the interface is up", nl)
        speed = _g("network_speed_mbps", "Negotiated link speed", nl)
        rx = _g("network_receive_bytes_total", "Bytes received", nl)
        tx = _g("network_transmit_bytes_total", "Bytes transmitted", nl)
        rx_err = _g("network_receive_errors_total", "Receive errors", nl)
        tx_err = _g("network_transmit_errors_total", "Transmit errors", nl)
        rx_drop = _g("network_receive_dropped_total", "Receive drops", nl)
        tx_drop = _g("network_transmit_dropped_total", "Transmit drops", nl)

        for iface in snap.network:
            labels = [node, iface.name]
            up.add_metric(labels, 1.0 if iface.up else 0.0)
            if iface.speed_mbps is not None:
                speed.add_metric(labels, float(iface.speed_mbps))
            rx.add_metric(labels, float(iface.rx_bytes_total))
            tx.add_metric(labels, float(iface.tx_bytes_total))
            rx_err.add_metric(labels, float(iface.rx_errors))
            tx_err.add_metric(labels, float(iface.tx_errors))
            rx_drop.add_metric(labels, float(iface.rx_dropped))
            tx_drop.add_metric(labels, float(iface.tx_dropped))

        yield from (up, speed, rx, tx, rx_err, tx_err, rx_drop, tx_drop)

    if snap.rdma:
        # Byte totals are exported, not the rates the live view computes:
        # Prometheus derives its own rate() and a pre-computed one would be
        # wrong at any step other than the one it was sampled at.
        rl = ["node", "device", "port"]
        active = _g("rdma_port_active", "1 when the RDMA port is ACTIVE", rl)
        rx = _g("rdma_receive_bytes_total", "Bytes received", rl)
        tx = _g("rdma_transmit_bytes_total", "Bytes transmitted", rl)
        errs = _g("rdma_errors_total", "Receive errors, discards and link downs", rl)
        info = _g(
            "rdma_port_info",
            "Always 1; carries link layer and negotiated rate as labels",
            [*rl, "link_layer", "rate"],
        )

        for port in snap.rdma:
            labels = [node, port.device, str(port.port)]
            active.add_metric(labels, 1.0 if port.active else 0.0)
            rx.add_metric(labels, float(port.rx_bytes_total))
            tx.add_metric(labels, float(port.tx_bytes_total))
            errs.add_metric(labels, float(port.errors))
            # An info-style metric: the rate string is what reveals a link that
            # negotiated below its rated speed, and it can't be a gauge value.
            info.add_metric([*labels, port.link_layer, port.rate], 1.0)

        yield from (active, rx, tx, errs, info)


def _process_metrics(snap: NodeSnapshot, node: str) -> Iterable[GaugeMetricFamily]:
    if not snap.processes:
        return

    # Unlabeled is a real category, not missing data: an unrecognized process
    # eating the pool is exactly what you want to see. Empty string rather than
    # a placeholder word keeps it queryable as `runtime=""`.
    labels = ["node", "runtime", "model", "router"]
    memory = _g("gpu_process_memory_bytes", "GPU memory held, by workload", labels)
    count = _g("gpu_process_count", "Processes holding GPU memory, by workload", labels)

    totals: dict[tuple[str, str, str], int] = {}
    counts: dict[tuple[str, str, str], int] = {}
    for proc in snap.processes:
        key = (proc.runtime or "", proc.model or "", proc.router or "")
        totals[key] = totals.get(key, 0) + proc.gpu_mem_bytes
        counts[key] = counts.get(key, 0) + 1

    for (runtime, model, router), total in sorted(totals.items()):
        memory.add_metric([node, runtime, model, router], float(total))
        count.add_metric([node, runtime, model, router], float(counts[(runtime, model, router)]))

    yield memory
    yield count


def _runtime_metrics(snap: NodeSnapshot, node: str) -> Iterable[GaugeMetricFamily]:
    if snap.runtimes.llama_cpp:
        # Every series carries a `router` label: a node runs several router
        # containers, and the same model name can be registered with more than
        # one of them.
        rl = ["node", "router"]
        rml = ["node", "router", "model"]

        up = _g("llama_router_up", "1 when the router answered", rl)
        known = _g("llama_models_known", "Models registered with the router", rl)
        active_count = _g("llama_models_active", "Models with weights resident", rl)
        sleeping_count = _g("llama_models_sleeping", "Models slept (process alive)", rl)
        capacity = _g("llama_router_max_instances", "--models-max ceiling", rl)

        # One series per state, matching the pattern used for clock and health:
        # alerting rules match on the label rather than decoding an enum.
        state = _g("llama_model_state", "Model lifecycle state (1 for active)", [*rml, "state"])
        tps = _g("llama_model_tokens_per_second", "Token throughput", rml)
        kv = _g("llama_model_kv_cache_percent", "KV cache utilization", rml)
        running = _g("llama_model_requests_running", "In-flight requests", rml)
        waiting = _g("llama_model_requests_waiting", "Queued requests", rml)

        for router in snap.runtimes.llama_cpp:
            label = router.name or router.endpoint
            up.add_metric([node, label], 1.0 if router.reachable else 0.0)
            known.add_metric([node, label], float(router.known_model_count))
            active_count.add_metric([node, label], float(len(router.active_models)))
            sleeping_count.add_metric([node, label], float(len(router.sleeping_models)))
            if router.max_instances is not None:
                capacity.add_metric([node, label], float(router.max_instances))

            for model in router.models:
                for value in ModelState:
                    state.add_metric(
                        [node, label, model.name, value.value],
                        1.0 if model.state is value else 0.0,
                    )
                # Throughput/cache series exist only for active models — a
                # sleeping model has no weights, and emitting 0 would be
                # indistinguishable from an idle-but-loaded model.
                if model.state is ModelState.ACTIVE:
                    tps.add_metric([node, label, model.name], model.tokens_per_sec or 0.0)
                    if model.kv_cache_pct is not None:
                        kv.add_metric([node, label, model.name], model.kv_cache_pct)
                    running.add_metric([node, label, model.name], float(model.requests_running))
                    waiting.add_metric([node, label, model.name], float(model.requests_waiting))

        yield from (
            up,
            known,
            active_count,
            sleeping_count,
            capacity,
            state,
            tps,
            kv,
            running,
            waiting,
        )

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
