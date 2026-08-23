"""Thin PromQL client for the history endpoints.

Deliberately thin. The live view never comes through here — it polls agents
directly — so this only serves "what did this look like over time", which is
exactly what Prometheus is good at and what a hand-rolled store would be bad at.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)


class PrometheusError(RuntimeError):
    """Prometheus was unreachable or returned an error."""


@dataclass(frozen=True)
class Series:
    """One time series: its labels and (timestamp, value) points."""

    labels: dict[str, str]
    points: list[tuple[float, float]]

    @property
    def node(self) -> str | None:
        return self.labels.get("node")


class PrometheusClient:
    def __init__(self, base_url: str, *, timeout_s: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    async def _get(self, path: str, params: dict) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.get(f"{self._base_url}{path}", params=params)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:
            raise PrometheusError(f"{type(exc).__name__}: {exc}") from exc

        if payload.get("status") != "success":
            raise PrometheusError(str(payload.get("error", "unknown error")))
        return payload.get("data", {})

    async def query(self, expr: str) -> list[Series]:
        """Instant query — current value per series."""
        data = await self._get("/api/v1/query", {"query": expr})
        out: list[Series] = []
        for result in data.get("result", []):
            value = result.get("value")
            points = [(float(value[0]), float(value[1]))] if value else []
            out.append(Series(labels=result.get("metric", {}), points=points))
        return out

    async def query_range(self, expr: str, start: float, end: float, step: str) -> list[Series]:
        """Range query — the shape the trend charts consume."""
        data = await self._get(
            "/api/v1/query_range",
            {"query": expr, "start": start, "end": end, "step": step},
        )
        out: list[Series] = []
        for result in data.get("result", []):
            points = [(float(t), float(v)) for t, v in result.get("values", [])]
            out.append(Series(labels=result.get("metric", {}), points=points))
        return out

    async def data_age_s(self) -> float | None:
        """Seconds since Prometheus last recorded a sample, or None if unknown.

        Being REACHABLE and being RECORDING are different things, and the gap
        between them is invisible without asking. On 2026-08-16 a clock step
        left this Prometheus answering queries perfectly while rejecting every
        incoming sample, so the dashboard reported "nothing firing" for half an
        hour with no data behind it.

        `up{job="prometheus"}` is the probe because Prometheus always scrapes
        itself: if that timestamp is not advancing, nothing is.
        """
        try:
            series = await self.query('time() - timestamp(up{job="prometheus"})')
        except Exception:  # noqa: BLE001 — unreachable is the caller's problem
            return None
        if not series:
            # The series aged out entirely, which means appends stopped long
            # enough ago that even the last sample went stale. Not "unknown".
            return float("inf")
        points = series[0].points
        if not points:
            return None
        return float(points[-1][1])

    async def healthy(self) -> bool:
        """Whether Prometheus is answering — feeds the backend's own /health."""
        try:
            await self._get("/api/v1/query", {"query": "up"})
        except PrometheusError:
            return False
        return True


# GB10 nodes only. node_exporter also runs ON the monitoring VM under its own
# job, and without this filter the VM would appear in every chart as an extra
# node the cluster does not contain — with no card, no colour slot and no
# business being compared against inference hardware.
_NODES = 'job="node-exporter"'

# `{window}` is filled in per request with a rate window scaled to the step —
# see RATE_WINDOW_STEPS. A fixed window is wrong at both ends: too long and a
# 1h chart smooths away the spike you opened it for, too short and a 7d chart
# samples 2 minutes out of every 10 and calls it a trend.

# Named queries the frontend asks for by key, so PromQL lives here rather than
# being assembled from user input in the request path.
HISTORY_QUERIES: dict[str, str] = {
    "gpu_utilization": "sparkdash_gpu_utilization_percent",
    "gpu_temperature": "sparkdash_gpu_temperature_celsius",
    "gpu_power": "sparkdash_gpu_power_watts",
    "gpu_clock": "sparkdash_gpu_clock_mhz",
    "memory_used_percent": (
        "100 * sparkdash_memory_used_bytes / sparkdash_memory_total_bytes"
    ),
    "memory_used_bytes": "sparkdash_memory_used_bytes",
    "cpu_utilization": "sparkdash_cpu_utilization_percent",
    "psi_some_avg10": "sparkdash_psi_memory_some_avg10",
    # `full`, not `some`. SOME means at least one task stalled waiting on
    # memory, which a busy inference box does routinely; FULL means EVERY
    # runnable task was stalled, i.e. nothing progressed at all. On a node whose
    # entire job is holding models in one shared pool, that is the difference
    # between "working hard" and "stopped", and the agent has been exporting it
    # all along with nothing plotting it.
    #
    # Same 10s window as its sibling above, deliberately: the two are meant to
    # be read side by side, and a `some` at 10s against a `full` at 60s would
    # invite comparing numbers that are not comparable.
    "psi_full_avg10": "sparkdash_psi_memory_full_avg10",
    # Cluster-wide throughput, summed across routers and every engine.
    #
    # ONE SUM OVER A NAME REGEX, not a sum per engine added together. Binary
    # `+` between instant vectors keeps only the label sets present on BOTH
    # sides, so `sum by (node) (llama) + sum by (node) (vllm)` — which is what
    # this was — returned NOTHING for a node running only one of them. Every
    # node here runs llama.cpp, so it read as correct; a vLLM-only or
    # SGLang-only node would have charted a flat blank while serving tokens.
    # A third engine would have made it strictly worse.
    #
    # Selecting the families by `__name__` and summing once has no matching
    # step at all, so a node contributes whatever it runs, and an engine added
    # later joins by name rather than by another term.
    # DECODE ONLY. The `_tokens_per_second` families are prefill and decode
    # added together, which spiked to 47,672 tok/s on a live cluster while the
    # model generated 48 — a large prompt landing inside one poll window is a
    # real ingest rate and is not what a throughput chart means. The combined
    # series is still recorded; it is just not what this chip plots.
    "tokens_per_second": (
        "sum by (node) ({__name__=~"
        '"sparkdash_(llama_model|vllm|sglang)_generation_tokens_per_second"})'
    ),
    # Prefill, offered separately rather than folded in. It answers "how fast
    # are requests being accepted", which is a real question and a different
    # one.
    "prompt_tokens_per_second": (
        "sum by (node) ({__name__=~"
        '"sparkdash_(llama_model|vllm|sglang)_prompt_tokens_per_second"})'
    ),
    # WHAT MONITORING COSTS, summed from every component that measures
    # ITSELF. Prometheus, Alertmanager and node_exporter export
    # `process_resident_memory_bytes` as standard; the agent now does too.
    #
    # SELF-REPORTED IS THE WHOLE DESIGN. The alternative — one collector
    # identifying "the monitoring processes" by name — is the ComfyUI problem
    # again: `python` names nothing, and a wrong match would bill someone's
    # model to monitoring, which is the exact number this is supposed to make
    # trustworthy.
    #
    # `spark-dash-agent` is excluded from the job filter deliberately: on a
    # multi-host install its RSS belongs to the node it runs on, not to the
    # monitoring host. The per-node figure is `agent_resident_memory_bytes`.
    "monitoring_bytes": (
        'sum(process_resident_memory_bytes{job=~"prometheus|alertmanager|'
        'node-exporter-central"})'
    ),
    # --- From node_exporter rather than the agent -------------------------
    #
    # These four carry a `node` label already, because the file_sd targets are
    # written with one — so they slot into the same per-node charts as the
    # agent's metrics with no joining.
    #
    # PSI counters are SECONDS STALLED, so the rate IS the fraction of wall
    # clock spent waiting; times 100 it is the same percentage the memory
    # pressure gauge reports. Aggregated by node even though there is one
    # series per node, which drops `instance` and `job` from the result and
    # leaves the clean label set the charts key on.
    "psi_cpu_some": (
        f"100 * max by (node) (rate(node_pressure_cpu_waiting_seconds_total"
        f"{{{_NODES}}}[{{window}}]))"
    ),
    "psi_io_some": (
        f"100 * max by (node) (rate(node_pressure_io_waiting_seconds_total"
        f"{{{_NODES}}}[{{window}}]))"
    ),
    # AVERAGED across cores, not maxed. Throttling shows up as every core
    # dropping together; a max would be held up by whichever core happened to
    # boost and would hide exactly the condition this exists to catch.
    "cpu_clock": f"avg by (node) (node_cpu_scaling_frequency_hertz{{{_NODES}}}) / 1e6",
    # MAXED across devices, not averaged. Saturation is "is any disk pegged",
    # and averaging a busy disk against an idle one reports a comfortable 50%
    # for a machine that is completely stalled on one of them.
    # Swap traffic, not swap OCCUPANCY. A node can hold gigabytes of cold pages
    # swapped out and be perfectly healthy; thrashing is a RATE. This plots the
    # exact quantity `SwapThrashing` alerts on (> 50 for 10m), so the chart and
    # the alert cannot disagree about what thrashing is.
    #
    # Pages per second, summed across in and out: direction does not matter to
    # the reader, only that pages are moving.
    "swap_io": (
        f"max by (node) (rate(node_vmstat_pswpin{{{_NODES}}}[{{window}}]) "
        f"+ rate(node_vmstat_pswpout{{{_NODES}}}[{{window}}]))"
    ),
    # --- Network, per INTERFACE rather than per node ----------------------
    #
    # THE ONLY HISTORY QUERIES WITH A SECOND DIMENSION. Every other entry here
    # yields one series per node; these yield one per interface, 14 on this
    # cluster against 3 nodes. That is deliberate and cannot be summed away: a
    # 200Gb RoCE link and a 10Gb management port added together is a number
    # describing nothing.
    #
    # BITS, because network gear is rated in bits and the Network table above
    # these charts already converts. A chart in bytes would disagree with the
    # table it sits under, at a factor of eight, with nothing on either saying
    # which was which.
    #
    # `rate()` OVER A GAUGE FAMILY, which looks wrong and is correct. These are
    # real monotonic counters read from sysfs, but the agent exports every
    # metric through GaugeMetricFamily, so Prometheus records them as gauges:
    # Grafana will not suggest `rate()` here and a PromQL linter will object to
    # it. They reset only on host reboot, which `rate()` handles as a counter
    # reset regardless of the declared type. Already noted in
    # central/grafana/README.md; repeated here because this is the first
    # history query that needs it.
    #
    # `sum by (node, interface)` over a single series per interface, purely to
    # drop `instance` and `job` and leave the label set the charts key on.
    "network_rx_bits": (
        "8 * sum by (node, interface) "
        "(rate(sparkdash_network_receive_bytes_total[{window}]))"
    ),
    "network_tx_bits": (
        "8 * sum by (node, interface) "
        "(rate(sparkdash_network_transmit_bytes_total[{window}]))"
    ),
    # Errors and drops, kept apart. A drop is usually backpressure — a queue
    # that overflowed under load — while an error is usually physical, a bad
    # cable or a failing transceiver. Summing them into one "faults" line would
    # save a chart and lose the distinction that decides whether anyone needs to
    # walk to the rack.
    #
    # ADDED WITH `+`, and this file argues against exactly that a few entries
    # up — so the difference is worth stating rather than looking like an
    # oversight.
    #
    # The name-regex trick used for tokens_per_second does not work here, and
    # fails loudly: `rate()` DROPS `__name__`, so
    # `rate({__name__=~"..._(receive|transmit)_errors_total"}[w])` reduces two
    # families to two series with the identical label set {node, interface},
    # and Prometheus refuses with "vector cannot contain metrics with the same
    # labelset". Measured, not reasoned about — it returned a 422. The regex
    # form only works for tokens_per_second because nothing there takes a rate,
    # so `__name__` survives to keep the series distinct.
    #
    # `+` is safe here for a reason that does not hold there. Engines are
    # OPTIONAL and per-node: a node running llama.cpp and not vLLM has one side
    # of the addition and not the other, so `+` drops it. Directions are not
    # optional. The agent emits receive and transmit for every interface in one
    # loop, unconditionally, so the two sides always carry the same label sets.
    # Pinned by test_both_directions_are_always_emitted, because that invariant
    # is what makes this expression correct.
    "network_errors": (
        "sum by (node, interface) "
        "(rate(sparkdash_network_receive_errors_total[{window}])) + "
        "sum by (node, interface) "
        "(rate(sparkdash_network_transmit_errors_total[{window}]))"
    ),
    "network_drops": (
        "sum by (node, interface) "
        "(rate(sparkdash_network_receive_dropped_total[{window}])) + "
        "sum by (node, interface) "
        "(rate(sparkdash_network_transmit_dropped_total[{window}]))"
    ),
    # RDMA port state, joined to the interface it shares a cable with.
    #
    # THE ONE FABRIC QUESTION THE THROUGHPUT CHARTS CANNOT ANSWER. A RoCE port
    # that dropped and came back at 03:00 leaves the byte counters looking like
    # an ordinary quiet spell; the only place it shows is here. Measured on this
    # cluster over 7d: four ports flapped 3-4 times each while every throughput
    # chart looked unremarkable.
    #
    # `group_left(interface)` IS WHY AC1c HAD TO SHIP FIRST. Without that label
    # the state series names a device (`roceP2p1s0f1`) and nothing can say which
    # wire that is, so the chart could only sit in a section of its own, away
    # from the traffic it explains. With it, the join carries the pairing
    # through and the chart lands beside its own interface.
    #
    # An agent that has not been upgraded yet simply contributes no `interface`
    # label, and the join still returns the state — degraded to "unpaired",
    # never dropped.
    #
    # `max by (...)` for the same reason the network queries aggregate: it
    # collapses `instance`, `job` and `cluster`. That last one matters more than
    # it looks. Measured over 7d here, 12 of 18 node/interface pairs have TWO
    # series because a `cluster` label was added part way through the window —
    # sequential, never overlapping (max concurrent series per key is 1), so no
    # aggregation double-counts today. `max` rather than `sum` keeps that true
    # if they ever do overlap: summing two 1s would give 2, which on a 0/1 axis
    # is off the top of the chart. Where both are present, up wins — a transient
    # duplicate should not be able to invent an outage.
    "rdma_port_state": (
        "max by (node, device, port, interface) (sparkdash_rdma_port_active"
        " * on (node, device, port) group_left(interface)"
        " sparkdash_rdma_port_info)"
    ),
    "disk_busy": (
        f"100 * max by (node) (rate(node_disk_io_time_seconds_total"
        f"{{{_NODES}}}[{{window}}]))"
    ),
}

# Metrics whose expression is a bare selector, and so can take a `{node="..."}`
# matcher appended. Everything else is an aggregation or arithmetic, where
# appending one is not valid PromQL — `(sum by (node) (x)){node="y"}` does not
# parse — and would have produced a 503 from Prometheus rather than an honest
# 400 from here.
NODE_FILTERABLE: frozenset[str] = frozenset(
    {
        "gpu_utilization",
        "gpu_temperature",
        "gpu_power",
        "gpu_clock",
        "memory_used_bytes",
        "cpu_utilization",
        "psi_some_avg10",
        "psi_full_avg10",
    }
)


#: How long a target must be down before the dashboard treats it as
#: "configured but absent" rather than "currently failing".
#:
#: 24h to match InferenceTargetScrapeFailing's age-out exactly. That alert
#: stops firing at 24h on the assumption the endpoint was retired; this is the
#: thing that must take over at precisely that moment, or the fact would stop
#: nagging AND be forgotten — which is the failure the age-out was allowed on
#: the promise of avoiding.
ABSENT_AFTER_S = 24 * 3600

#: Currently failing to scrape.
TARGETS_DOWN = "up == 0"

#: Seconds since each target was last seen up.
#:
#: `timestamp()` goes INSIDE the subquery deliberately. Applied outside — over
#: `last_over_time` — it reports the time of the EVALUATION rather than of the
#: sample, so every target came back as "last up 0 seconds ago" including ones
#: that had been down for days. Measured, not assumed: it read 0.0h for a
#: target that was genuinely 32.5h down.
#:
#: A target that has never been up in the window yields no series at all, which
#: is why the join happens in Python — a never-up target is exactly the typo'd
#: port this feature exists to surface, and a PromQL join would drop it.
TARGET_LAST_UP = "time() - max_over_time(timestamp(up == 1)[30d:10m])"


def step_seconds(step: str) -> float:
    """A Prometheus step string as seconds. Falls back to 60s, the default.

    Shared because several things must agree on it: the rate window below, and
    the alert-episode gap tolerance, which fragments one continuous alert into
    one episode per sample if it is smaller than the step.
    """
    digits = "".join(c for c in step if c.isdigit() or c == ".")
    try:
        value = float(digits)
    except ValueError:
        return 60.0
    if not value:
        return 60.0
    if step.endswith("m"):
        return value * 60
    if step.endswith("h"):
        return value * 3600
    return value


def rate_window(step: str) -> str:
    """A rate window matched to the step the chart is drawn at.

    Four steps wide: enough samples to be smooth at a 15s scrape, short enough
    that a point still describes the interval it sits on rather than a smear of
    the ones around it. Floored at 1m because a window shorter than a couple of
    scrapes yields nothing at all.
    """
    return f"{max(60, int(step_seconds(step) * 4))}s"
