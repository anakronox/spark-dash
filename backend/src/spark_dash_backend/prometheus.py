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
}
