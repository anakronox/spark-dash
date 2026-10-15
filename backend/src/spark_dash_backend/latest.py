"""The latest reading of the metrics the live snapshot does not carry (roadmap BA2).

System Activity's gauges read the snapshot, which arrives every 1-2s. Five of
the metrics it charts come from node_exporter through Prometheus and never
reach the snapshot: the CPU clock, swap traffic, CPU and I/O pressure, and disk
busy. Their gauges take Prometheus's latest value instead, with the time it was
scraped, so the page can say how old the reading is rather than present a
15-second-old figure as live.

The same named expressions as the charts, so a gauge and the line beside it
measure one quantity.
"""

from __future__ import annotations

import asyncio
import math

from .prometheus import HISTORY_QUERIES, PrometheusClient, PrometheusError

#: The metrics this serves. Everything else on the card is live.
LATEST_METRICS: frozenset[str] = frozenset(
    {"cpu_clock", "swap_io", "psi_cpu_some", "psi_io_some", "disk_busy"}
)

#: Four scrapes at 15s: the shortest window `rate()` is reliable over, and the
#: one the charts use at their finest step.
RATE_WINDOW = "1m"

#: When node_exporter was last scraped on each node, which is when every one
#: of these five was sampled.
SAMPLED_AT = 'max by (node) (timestamp(up{job="node-exporter"}))'


async def latest(prom: PrometheusClient, keys: list[str]) -> dict:
    """`{"values": {metric: {node: value}}, "sampled_at": {node: unix_s}}`.

    A metric whose query fails is left out rather than failing the rest: the
    gauge for it reads "—", and the others still move.
    """
    wanted = [k for k in dict.fromkeys(keys) if k in LATEST_METRICS]

    async def one(key: str) -> tuple[str, dict[str, float] | None]:
        expr = HISTORY_QUERIES[key].replace("{window}", RATE_WINDOW)
        try:
            series = await prom.query(expr)
        except PrometheusError:
            return key, None
        out: dict[str, float] = {}
        for s in series:
            if s.node and s.points and not math.isnan(s.points[-1][1]):
                out[s.node] = s.points[-1][1]
        return key, out

    async def stamps() -> dict[str, float]:
        try:
            series = await prom.query(SAMPLED_AT)
        except PrometheusError:
            return {}
        return {s.node: s.points[-1][1] for s in series if s.node and s.points}

    results, sampled = await asyncio.gather(
        asyncio.gather(*(one(k) for k in wanted)), stamps()
    )
    return {
        "values": {k: v for k, v in results if v is not None},
        "sampled_at": sampled,
    }
