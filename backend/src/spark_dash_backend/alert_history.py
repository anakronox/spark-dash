"""Alert history — what fired, what nearly fired, and for how long.

Reconstructed from Prometheus rather than stored separately, for the same
reason `timeline.py` is: the TSDB already has it. Prometheus writes
`ALERTS{alertname,alertstate,severity,...}` for every alert that is pending or
firing, so the record exists whether or not anything reads it — and history
reaches back as far as retention rather than starting from whenever this
shipped.

Alertmanager is NOT the source here. It knows what is firing now and keeps no
useful history, which is exactly the gap this fills. `/api/alerts` remains the
live view; this is the record.

PENDING EPISODES ARE FIRST-CLASS, not noise to be filtered. When this was
written, every ALERTS series in the previous week was `pending` and nothing had
ever reached `firing` — because several rules had `for:` windows longer than
the events they watched. An alert that repeatedly goes pending and never fires
is the clearest possible signal that its rule is mistuned, and a view that
showed only firing alerts would have shown an empty page while that was true.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from spark_dash_backend.prometheus import PrometheusClient, PrometheusError, step_seconds

log = logging.getLogger(__name__)

ALERTS_METRIC = "ALERTS"

# Labels Prometheus adds that describe the series rather than the alert.
_META_LABELS = frozenset({"__name__", "alertstate"})

# How long a break in samples is treated as the alert having stopped, rather
# than as a scrape that went missing.
#
# Two different things produce a gap and they must not be conflated: the alert
# genuinely resolving, and Prometheus restarting (or failing to evaluate) while
# the alert was still active. Restarts were observed repeatedly during the
# 2026-08-16 deploys, each one splitting what was really one continuous episode.
# A tolerance of a few evaluation intervals bridges the restart without
# swallowing a real resolve-and-refire.
DEFAULT_GAP_TOLERANCE_S = 150.0


@dataclass
class AlertEpisode:
    """One continuous period during which an alert was pending and/or firing."""

    alertname: str
    severity: str
    node: str | None
    started_at: float
    ended_at: float
    #: True while the episode is still going at the end of the window.
    ongoing: bool
    #: Whether it ever got past `for:` and actually fired.
    fired: bool
    #: When it first reached `firing`, if it ever did.
    fired_at: float | None
    labels: dict[str, str]
    #: Overlapped a maintenance window on its node. Context, not erasure:
    #: the episode still happened. Set after extraction by
    #: `maintenance.tag_episodes`, which is why it is not frozen.
    maintenance: bool = False

    @property
    def duration_s(self) -> float:
        return max(0.0, self.ended_at - self.started_at)

    def as_dict(self) -> dict:
        return {
            "alertname": self.alertname,
            "severity": self.severity,
            "node": self.node,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_s": self.duration_s,
            "ongoing": self.ongoing,
            "fired": self.fired,
            "fired_at": self.fired_at,
            "labels": self.labels,
            "maintenance": self.maintenance,
        }


def _identity(labels: dict[str, str]) -> tuple:
    """What makes two samples the same alert.

    Everything except `alertstate`, because pending and firing are two states
    of ONE episode rather than two separate events — an alert crosses from one
    to the other when its `for:` elapses, and reporting that as two rows would
    double-count every alert that ever fired.
    """
    return tuple(sorted((k, v) for k, v in labels.items() if k not in _META_LABELS))


def extract_episodes(
    series: list,
    *,
    window_end: float,
    gap_tolerance_s: float = DEFAULT_GAP_TOLERANCE_S,
) -> list[AlertEpisode]:
    """Turn ALERTS samples into episodes.

    ALERTS exists only while an alert is pending or firing — it is absent, not
    zero, the rest of the time. So an episode is a run of consecutive samples,
    and the boundaries are found by looking for breaks longer than
    `gap_tolerance_s`.
    """
    # identity -> [(ts, alertstate), ...]
    samples: dict[tuple, list[tuple[float, str]]] = {}
    labels_by_identity: dict[tuple, dict[str, str]] = {}

    for s in series:
        labels = dict(getattr(s, "labels", {}) or {})
        state = labels.get("alertstate", "")
        identity = _identity(labels)
        labels_by_identity.setdefault(
            identity, {k: v for k, v in labels.items() if k not in _META_LABELS}
        )
        for ts, value in getattr(s, "points", []):
            # ALERTS carries 1 while active. Anything else isn't a live sample.
            if value == 1.0:
                samples.setdefault(identity, []).append((ts, state))

    episodes: list[AlertEpisode] = []
    for identity, points in samples.items():
        points.sort(key=lambda p: p[0])
        labels = labels_by_identity[identity]

        run: list[tuple[float, str]] = []
        for point in points:
            if run and point[0] - run[-1][0] > gap_tolerance_s:
                episodes.append(_episode(run, labels, window_end, gap_tolerance_s))
                run = []
            run.append(point)
        if run:
            episodes.append(_episode(run, labels, window_end, gap_tolerance_s))

    episodes.sort(key=lambda e: e.started_at, reverse=True)
    return episodes


def _episode(
    run: list[tuple[float, str]],
    labels: dict[str, str],
    window_end: float,
    gap_tolerance_s: float,
) -> AlertEpisode:
    started = run[0][0]
    ended = run[-1][0]
    firing = [ts for ts, state in run if state == "firing"]

    return AlertEpisode(
        alertname=labels.get("alertname", "?"),
        severity=labels.get("severity", "none"),
        node=labels.get("node"),
        started_at=started,
        ended_at=ended,
        # Still running if its last sample is recent enough that the next one
        # simply hasn't been scraped yet.
        ongoing=(window_end - ended) <= gap_tolerance_s,
        fired=bool(firing),
        fired_at=min(firing) if firing else None,
        labels=labels,
    )


def summarise(episodes: list[AlertEpisode]) -> dict:
    """Headline counts.

    `pending_only` is called out separately because it is the number that says
    a rule is mistuned: the condition kept being met but never for long enough
    to matter, which is either a threshold set too tight or a `for:` set too
    long.
    """
    fired = [e for e in episodes if e.fired]
    return {
        "episodes": len(episodes),
        "fired": len(fired),
        "pending_only": len(episodes) - len(fired),
        "ongoing": len([e for e in episodes if e.ongoing]),
        # Of the fired ones, how many were expected. "3 fired, 2 during
        # maintenance" is a different week from "3 fired".
        "during_maintenance": len([e for e in fired if e.maintenance]),
    }


async def fetch_episodes(
    prom: PrometheusClient,
    *,
    start: float,
    end: float,
    step: str = "60s",
    gap_tolerance_s: float | None = None,
) -> list[AlertEpisode]:
    """Read alert episodes over a window.

    THE TOLERANCE SCALES WITH THE STEP, and it has to. A gap cannot be smaller
    than the sampling resolution, so a fixed 150s tolerance means that at any
    step above that EVERY sample looks like the start of a new episode. The
    constraint was documented here and not enforced, and it showed up the first
    time something queried a 7-day window at its natural 3600s step: one
    continuous alert came back as 21 separate episodes, exactly one per sample,
    and drew 21 marks on the history charts where there was one incident.

    2.5 steps keeps the original intent — bridge a couple of missed evaluations
    without swallowing a real resolve-and-refire — at whatever resolution the
    caller is working in. An explicit value still wins, for callers that know
    better.
    """
    if gap_tolerance_s is None:
        gap_tolerance_s = max(DEFAULT_GAP_TOLERANCE_S, 2.5 * step_seconds(step))
    try:
        series = await prom.query_range(ALERTS_METRIC, start, end, step)
    except PrometheusError:
        log.debug("alert history query failed", exc_info=True)
        raise

    return extract_episodes(series, window_end=end, gap_tolerance_s=gap_tolerance_s)
