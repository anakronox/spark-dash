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
    # Cluster-wide throughput, summed across routers and vLLM instances.
    "tokens_per_second": (
        "sum by (node) (sparkdash_llama_model_tokens_per_second) "
        "+ sum by (node) (sparkdash_vllm_tokens_per_second)"
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
    }
)


def rate_window(step: str) -> str:
    """A rate window matched to the step the chart is drawn at.

    Four steps wide: enough samples to be smooth at a 15s scrape, short enough
    that a point still describes the interval it sits on rather than a smear of
    the ones around it. Floored at 1m because a window shorter than a couple of
    scrapes yields nothing at all.
    """
    try:
        seconds = int("".join(ch for ch in step if ch.isdigit()))
    except ValueError:
        seconds = 60
    if step.endswith("m"):
        seconds *= 60
    elif step.endswith("h"):
        seconds *= 3600
    return f"{max(60, seconds * 4)}s"
