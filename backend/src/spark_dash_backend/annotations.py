"""Events worth drawing on the history charts.

WHY THIS EXISTS. The charts answer "what happened"; three other views answer
"why". Reading a throughput dip used to mean opening the model timeline, the
alert history and the agent build list, then aligning three timestamps by eye.
Drawing the events on the axis they already share collapses that into one
glance: a dip arrives with its candidate explanation attached.

WHAT IS DRAWN, AND WHY NOT EVERYTHING. Measured on a real 7-day window: 82
model swaps, 78 alert episodes, 13 deploys — 173 events on a chart about 390px
wide, which is one every 2.3px. That is not an annotation layer, it is a grey
wash that hides the data underneath it.

So the filter is not a density cap picked to fit; it is a question about which
events could plausibly EXPLAIN a change in the line:

  - Alerts that FIRED. An episode that only ever went pending means its rule is
    mistuned, which is worth knowing and is not an event on the hardware.
  - COLD model starts. A warm sleep/wake costs almost nothing; a cold start
    reads weights back off disk and is the one that shows up as a latency
    spike. This is already the number the timeline calls out.
  - DEPLOYS. A metric that changes shape right after the agent was replaced is
    a different story from one that changed on its own, and it is the first
    thing to rule out.

The same window then yields 6 + 4 + 13 = 23. The full lists remain a click away
in the model timeline and the alert history — this layer is for correlation,
not for enumeration.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from spark_dash_backend.alert_history import fetch_episodes
from spark_dash_backend.prometheus import PrometheusClient, step_seconds
from spark_dash_backend.timeline import fetch_events

log = logging.getLogger(__name__)

#: Series carrying the agent's build. One series per build label, so the first
#: sample of each is the moment that build started running.
BUILD_INFO = "sparkdash_agent_build_info"


@dataclass(frozen=True)
class Annotation:
    """One event, at one instant, on the shared time axis."""

    ts: float
    #: Drives the colour and the wording. Kept coarse deliberately — three
    #: kinds a reader can hold in their head, not a taxonomy.
    kind: str  # "alert" | "cold-start" | "deploy" | "maintenance"
    label: str
    node: str | None = None
    #: Only a maintenance window has one. The other kinds are instants; a
    #: window is a stretch, and drawing it as a tick at its start would put a
    #: mark where the reader wants a band.
    end_ts: float | None = None


async def fetch_annotations(
    prom: PrometheusClient,
    *,
    start: float,
    end: float,
    step: str = "60s",
) -> list[Annotation]:
    """Every drawable event in the window, oldest first."""
    out: list[Annotation] = []

    try:
        for episode in await fetch_episodes(prom, start=start, end=end, step=step):
            # `fired_at`, not `started_at`: the pending period is the rule
            # waiting out its `for:`, and the condition is what the reader is
            # correlating against.
            if not episode.fired or episode.fired_at is None:
                continue
            out.append(
                Annotation(
                    ts=episode.fired_at,
                    kind="alert",
                    label=episode.alertname,
                    node=episode.node,
                )
            )
    except Exception:  # noqa: BLE001 — one source failing must not blank the rest
        log.warning("alert episodes unavailable for annotations", exc_info=True)

    try:
        for event in await fetch_events(prom, start=start, end=end, step=step):
            if not event.cold:
                continue
            out.append(
                Annotation(
                    ts=event.ts,
                    kind="cold-start",
                    label=f"{event.model} cold start",
                    node=event.node,
                )
            )
    except Exception:  # noqa: BLE001
        log.warning("model timeline unavailable for annotations", exc_info=True)

    try:
        out.extend(await _deploys(prom, start=start, end=end, step=step))
    except Exception:  # noqa: BLE001
        log.warning("build info unavailable for annotations", exc_info=True)

    try:
        out.extend(await _maintenance(prom, start=start, end=end, step=step))
    except Exception:  # noqa: BLE001
        log.warning("maintenance record unavailable for annotations", exc_info=True)

    out.sort(key=lambda a: a.ts)
    return out


async def _deploys(
    prom: PrometheusClient, *, start: float, end: float, step: str
) -> list[Annotation]:
    """When each agent build started running.

    `sparkdash_agent_build_info` carries the commit as a LABEL with a constant
    value, so there is one series per build and the first sample of each is the
    moment it appeared.

    A build whose first sample sits at the very start of the window was already
    running when the window opened — it did not deploy inside it. Reporting one
    would put a phantom marker on the left edge of every chart, every time.
    """
    series = await prom.query_range(BUILD_INFO, start, end, step)
    step_s = step_seconds(step)

    out: list[Annotation] = []
    for one in series:
        if not one.points:
            continue
        first = one.points[0][0]
        if first - start <= step_s:
            continue
        build = one.labels.get("build", "unknown")
        out.append(
            Annotation(
                ts=first,
                kind="deploy",
                label=f"agent {build}",
                node=one.labels.get("node"),
            )
        )
    return out


async def _maintenance(
    prom: PrometheusClient, *, start: float, end: float, step: str
) -> list[Annotation]:
    """Maintenance windows, one band each.

    The record is per NODE (one series per member), but a reader declared one
    window, so members of a cluster-scope window collapse to a single band
    with no node — the band is about the cluster. A node-scope window keeps
    its node so the chart can tint it.

    Not filtered like alerts are: a dip that happened inside a window arrives
    with its explanation attached, which is the whole point of this layer.
    """
    from spark_dash_backend.maintenance import fetch_intervals

    seen: dict[tuple[str, float], Annotation] = {}
    for iv in await fetch_intervals(prom, start=start, end=end, step=step):
        key = (iv.window or f"{iv.scope}:{iv.name}", iv.started_at)
        current = seen.get(key)
        if current is None:
            seen[key] = Annotation(
                ts=iv.started_at,
                kind="maintenance",
                label=f"{iv.name} maintenance",
                node=iv.node if iv.scope == "node" else None,
                end_ts=iv.ended_at,
            )
        elif iv.ended_at > (current.end_ts or 0):
            seen[key] = Annotation(
                ts=current.ts,
                kind=current.kind,
                label=current.label,
                node=current.node,
                end_ts=iv.ended_at,
            )
    return list(seen.values())


def as_dicts(annotations: list[Annotation]) -> list[dict]:
    return [asdict(a) for a in annotations]
