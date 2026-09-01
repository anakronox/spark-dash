"""Maintenance windows — saying "this is planned" before the alert fires.

WHAT A WINDOW IS. A pre-emptive Alertmanager silence with a name. G2 made it
possible to say "yes, that was me" after an alert fired; by then ntfy has
already paged you. Planned work — trying another model, changing a vLLM run
flag — wants the other order: declare the scope first, then stop the
container. A silence matched on the `node` label covers every rule that names
a node, including the ones not written yet, and it is effective the instant it
is created. That immediacy is why this is a silence and not a synthetic
inhibiting alert: an inhibit would lag a scrape plus an evaluation, and the
whole point is to be covered BEFORE the container stops.

ALERTMANAGER IS THE ONLY STORE. A window is recognised by its author
(`spark-dash/maintenance`) and a structured comment. Reading the active
windows is filtering Alertmanager's silences; there is no file or table that
could disagree with what is actually muting alerts, and the state survives a
backend restart because Alertmanager persists its own silences.

CLUSTERS MUTE TOGETHER. Cluster scope covers every member. Node scope on a
cluster member ALSO mutes the peer-comparison alerts for its cluster
(`ClusterNodeRunningHot`, `ClusterNodeClockLagging`): those compare a node to
the coolest or fastest member, so an idle node under maintenance makes the
working peer read hot — an alert on a node nobody touched. That second silence
belongs to the same window and ends with it.

WHAT IS DELIBERATELY NOT COVERED. Alerts without a `node` label — Prometheus
self-monitoring, `AgentBuildSkew` — are untouched. Maintenance on a GX10 says
nothing about the monitoring VM.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from spark_dash_common.models import ClusterSnapshot, MaintenanceMark

log = logging.getLogger(__name__)

#: `createdBy` on every silence a window owns. What makes them recognisable.
AUTHOR = "spark-dash/maintenance"

#: Brian, 2026-09-02. Long enough for a model swap and a look at the result;
#: short enough that a forgotten window is over by the evening.
DEFAULT_HOURS = 4.0

#: The rules that compare a node to its cluster peers. Muted for the whole
#: cluster when one member goes into maintenance — see the module header.
PEER_ALERTS = "ClusterNode.*"

Scope = Literal["node", "cluster"]

#: Exported for Prometheus, so the record of a window outlives the silence
#: that made it. Alertmanager forgets silences after five days; the TSDB keeps
#: this for as long as it keeps everything else.
METRIC = "sparkdash_maintenance"

# Comment grammar. Readable on Alertmanager's own UI at :9093 AND parseable
# back, which is why it is not JSON:
#
#   maintenance node sparky: trying Qwen3-235B [window 1a2b3c4d]
#   maintenance node sparky (peers): trying Qwen3-235B [window 1a2b3c4d]
#   maintenance cluster danflashes: [window 9f8e7d6c]
_COMMENT = re.compile(
    r"^maintenance (node|cluster) (\S+?)( \(peers\))?: ?(.*?) ?\[window ([0-9a-f]+)\]$"
)


class ScopeError(ValueError):
    """The scope names nothing the inventory knows."""


@dataclass(frozen=True)
class ResolvedScope:
    scope: Scope
    name: str
    #: Every node the window covers.
    nodes: list[str]
    #: For node scope, the cluster the node belongs to (peers get muted too).
    cluster: str | None = None


def resolve_scope(scope: str, name: str, inventory_nodes) -> ResolvedScope:
    """Turn `scope` + `name` into the nodes it covers.

    Through the inventory rather than cluster.yml directly, so the SPARK_NODES
    path resolves identically — the inventory is what the poller and the
    target renderer already agree on.
    """
    name = name.strip()
    if scope == "node":
        match = next((n for n in inventory_nodes if n.node_id == name), None)
        if match is None:
            raise ScopeError(f"no node {name!r} in the inventory")
        return ResolvedScope("node", name, [name], cluster=match.cluster)
    if scope == "cluster":
        members = [n.node_id for n in inventory_nodes if n.cluster == name]
        if not members:
            raise ScopeError(f"no cluster {name!r} in the inventory")
        return ResolvedScope("cluster", name, members, cluster=name)
    raise ScopeError(f"scope must be node or cluster, not {scope!r}")


def _regex_alternation(values: list[str]) -> str:
    return "|".join(re.escape(v) for v in values)


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def matcher_sets(resolved: ResolvedScope) -> list[tuple[bool, list[dict]]]:
    """The silences a window needs, as (is_peers, matchers).

    One per silence: Alertmanager ANDs the matchers inside a silence, so the
    node coverage and the peer coverage cannot share one.
    """
    if resolved.scope == "node":
        primary = [{"name": "node", "value": resolved.name, "isRegex": False, "isEqual": True}]
    else:
        primary = [
            {
                "name": "node",
                "value": _regex_alternation(resolved.nodes),
                "isRegex": True,
                "isEqual": True,
            }
        ]
    out: list[tuple[bool, list[dict]]] = [(False, primary)]

    if resolved.scope == "node" and resolved.cluster:
        out.append(
            (
                True,
                [
                    {
                        "name": "alertname",
                        "value": PEER_ALERTS,
                        "isRegex": True,
                        "isEqual": True,
                    },
                    {
                        "name": "cluster",
                        "value": resolved.cluster,
                        "isRegex": False,
                        "isEqual": True,
                    },
                ],
            )
        )
    return out


def comment_for(resolved: ResolvedScope, reason: str, window_id: str, *, peers: bool) -> str:
    tag = " (peers)" if peers else ""
    reason = " ".join(reason.split())  # one line, however it was typed
    return f"maintenance {resolved.scope} {resolved.name}{tag}: {reason} [window {window_id}]"


@dataclass(frozen=True)
class ParsedComment:
    scope: Scope
    name: str
    peers: bool
    reason: str
    window: str


def parse_comment(text: str | None) -> ParsedComment | None:
    m = _COMMENT.match(text or "")
    if not m:
        return None
    scope, name, peers, reason, window = m.groups()
    return ParsedComment(scope=scope, name=name, peers=bool(peers), reason=reason, window=window)  # type: ignore[arg-type]


@dataclass
class MaintenanceWindow:
    id: str
    scope: Scope
    name: str
    nodes: list[str]
    reason: str
    starts_at: str
    ends_at: str
    silence_ids: list[str] = field(default_factory=list)
    #: Alerts currently held by this window's silences.
    held: int = 0

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "scope": self.scope,
            "name": self.name,
            "nodes": list(self.nodes),
            "reason": self.reason,
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "silence_ids": list(self.silence_ids),
            "held": self.held,
        }

    def mark(self) -> MaintenanceMark:
        return MaintenanceMark(
            window=self.id,
            scope=self.scope,
            name=self.name,
            reason=self.reason,
            ends_at=datetime.fromisoformat(self.ends_at.replace("Z", "+00:00")),
        )


def _nodes_from_matchers(matchers: list[dict]) -> list[str]:
    for m in matchers:
        if m.get("name") != "node":
            continue
        value = str(m.get("value", ""))
        if m.get("isRegex"):
            return [_unescape(v) for v in value.split("|") if v]
        return [value]
    return []


def windows_from_silences(
    silences: list[dict], held: list[dict] | None = None
) -> list[MaintenanceWindow]:
    """Group a window's silences back into a window.

    Only silences this module wrote are considered — a hand-made silence on
    `:9093` that happens to match a node is a silence, not a window, and is
    still shown in the Silenced list like any other.
    """
    grouped: dict[str, list[tuple[ParsedComment, dict]]] = {}
    for s in silences:
        if s.get("createdBy") != AUTHOR:
            continue
        parsed = parse_comment(s.get("comment"))
        if parsed is None:
            continue
        grouped.setdefault(parsed.window, []).append((parsed, s))

    # silence id -> alerts it is holding. An alert held by two silences of the
    # same window is one alert, so windows count DISTINCT alerts below.
    holders: dict[str, set[str]] = {}
    for item in held or []:
        fingerprint = str(item.get("fingerprint") or id(item))
        for sid in (item.get("status") or {}).get("silencedBy") or []:
            holders.setdefault(str(sid), set()).add(fingerprint)

    out: list[MaintenanceWindow] = []
    for window_id, parts in grouped.items():
        primary = next(((p, s) for p, s in parts if not p.peers), None)
        if primary is None:
            # Peers silence outlived its primary (expired separately, or was
            # deleted by hand). Nothing is protecting the node, so this is not
            # a window any more; it will show as a plain silence.
            continue
        parsed, silence = primary
        ids = [str(s.get("id")) for _, s in parts]
        held_alerts: set[str] = set()
        for sid in ids:
            held_alerts |= holders.get(sid, set())
        out.append(
            MaintenanceWindow(
                id=window_id,
                scope=parsed.scope,
                name=parsed.name,
                nodes=_nodes_from_matchers(silence.get("matchers") or []),
                reason=parsed.reason,
                starts_at=str(silence.get("startsAt", "")),
                ends_at=str(silence.get("endsAt", "")),
                silence_ids=ids,
                held=len(held_alerts),
            )
        )
    out.sort(key=lambda w: w.starts_at, reverse=True)
    return out


def render_metrics(windows: list[MaintenanceWindow]) -> str:
    """Prometheus exposition: one series per node under a window.

    The reason is NOT a label. It is free text, and a label value that changes
    with every window is a new series every time — the cardinality trap the
    agent's exporter already avoids. The name and window id are enough to
    join back to a reason if anyone ever needs one.
    """
    lines = [
        f"# HELP {METRIC} 1 while the node is under a maintenance window "
        "declared from the dashboard.",
        f"# TYPE {METRIC} gauge",
    ]
    for w in windows:
        for node in w.nodes:
            labels = (
                f'node="{_label(node)}",scope="{w.scope}",'
                f'name="{_label(w.name)}",window="{w.id}"'
            )
            lines.append(f"{METRIC}{{{labels}}} 1")
    return "\n".join(lines) + "\n"


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class MaintenanceService:
    """Windows as the backend sees them: cached reads, immediate writes.

    Reads are cached for `ttl_s` because the live poller stamps every tick and
    must not ask Alertmanager at 2s; a stale cache is refreshed in the
    background rather than awaited, so a slow Alertmanager never delays a
    frame. Writes refresh synchronously — the card that just declared a
    window must show it on the very next frame, not up to a TTL later.
    """

    def __init__(self, alertmanager, inventory, *, ttl_s: float = 15.0) -> None:
        self._am = alertmanager
        self._inventory = inventory
        self._ttl_s = ttl_s
        self._windows: list[MaintenanceWindow] = []
        self._fetched_at: float = 0.0
        self._refreshing: asyncio.Task | None = None

    @property
    def cached(self) -> list[MaintenanceWindow]:
        return list(self._windows)

    def _stale(self) -> bool:
        loop = asyncio.get_running_loop()
        return loop.time() - self._fetched_at > self._ttl_s

    async def refresh(self) -> list[MaintenanceWindow]:
        silences = await self._am.silences()
        held = await self._am.silenced()
        self._windows = windows_from_silences(silences, held)
        self._fetched_at = asyncio.get_running_loop().time()
        return self.cached

    async def active(self) -> list[MaintenanceWindow]:
        """Fresh enough for an API answer: refreshes when the cache is stale."""
        if self._stale():
            await self.refresh()
        return self.cached

    def _kick(self) -> None:
        """Refresh in the background if stale and nothing is already doing so."""
        if not self._stale():
            return
        if self._refreshing is not None and not self._refreshing.done():
            return

        async def run() -> None:
            try:
                await self.refresh()
            except Exception:  # noqa: BLE001 — a refresh failing keeps the last view
                log.debug("maintenance refresh failed", exc_info=True)

        self._refreshing = asyncio.get_running_loop().create_task(run())

    def stamp(self, snapshot: ClusterSnapshot) -> None:
        """Mark every node under a window. Synchronous, from the cache."""
        self._kick()
        if not self._windows:
            return
        by_node: dict[str, MaintenanceMark] = {}
        for w in self._windows:
            for node_id in w.nodes:
                # Two windows on one node: the one ending later is the one
                # that matters to a reader deciding whether to wait.
                current = by_node.get(node_id)
                if current is None or w.mark().ends_at > current.ends_at:
                    by_node[node_id] = w.mark()
        for node in snapshot.nodes:
            node.maintenance = by_node.get(node.node_id)

    async def start(
        self, scope: str, name: str, *, hours: float, reason: str = ""
    ) -> MaintenanceWindow:
        resolved = resolve_scope(scope, name, self._inventory.nodes())
        window_id = secrets.token_hex(4)
        created: list[str] = []
        try:
            for peers, matchers in matcher_sets(resolved):
                created.append(
                    await self._am.create_silence(
                        matchers,
                        hours=hours,
                        comment=comment_for(resolved, reason, window_id, peers=peers),
                        author=AUTHOR,
                    )
                )
        except Exception:
            # Half a window is worse than none: the node would be muted while
            # its peers were not, and the reader would see one window. Undo
            # what was made, then let the caller see the failure.
            for sid in created:
                try:
                    await self._am.expire_silence(sid)
                except Exception:  # noqa: BLE001
                    log.warning("could not roll back silence %s", sid, exc_info=True)
            raise
        await self.refresh()
        match = next((w for w in self._windows if w.id == window_id), None)
        if match is None:  # Alertmanager accepted it but does not list it yet
            now = datetime.now(UTC)
            match = MaintenanceWindow(
                id=window_id,
                scope=resolved.scope,
                name=resolved.name,
                nodes=list(resolved.nodes),
                reason=reason,
                starts_at=now.isoformat(),
                ends_at=datetime.fromtimestamp(now.timestamp() + hours * 3600, UTC).isoformat(),
                silence_ids=created,
            )
        return match

    async def end(self, window_id: str) -> MaintenanceWindow:
        """Expire every silence in the window. The early way out."""
        windows = await self.active()
        match = next((w for w in windows if w.id == window_id), None)
        if match is None:
            raise KeyError(window_id)
        for sid in match.silence_ids:
            await self._am.expire_silence(sid)
        await self.refresh()
        return match


# ---------------------------------------------------------------- the record


@dataclass(frozen=True)
class MaintenanceInterval:
    """One node's stretch under one window, read back from Prometheus."""

    node: str
    scope: str
    name: str
    window: str
    started_at: float
    ended_at: float
    ongoing: bool


def extract_intervals(
    series: list, *, window_end: float, gap_tolerance_s: float
) -> list[MaintenanceInterval]:
    """Runs of samples, split on gaps — the same shape `alert_history` uses,
    for the same reason: the series exists only while the window does."""
    out: list[MaintenanceInterval] = []
    for s in series:
        labels = dict(getattr(s, "labels", {}) or {})
        points = sorted(ts for ts, value in getattr(s, "points", []) if value == 1.0)
        runs: list[list[float]] = []
        for ts in points:
            if runs and ts - runs[-1][-1] <= gap_tolerance_s:
                runs[-1].append(ts)
            else:
                runs.append([ts])
        for run in runs:
            out.append(
                MaintenanceInterval(
                    node=labels.get("node", "?"),
                    scope=labels.get("scope", "node"),
                    name=labels.get("name", "?"),
                    window=labels.get("window", ""),
                    started_at=run[0],
                    ended_at=run[-1],
                    ongoing=(window_end - run[-1]) <= gap_tolerance_s,
                )
            )
    out.sort(key=lambda i: i.started_at)
    return out


async def fetch_intervals(
    prom, *, start: float, end: float, step: str
) -> list[MaintenanceInterval]:
    from spark_dash_backend.alert_history import DEFAULT_GAP_TOLERANCE_S
    from spark_dash_backend.prometheus import step_seconds

    tolerance = max(DEFAULT_GAP_TOLERANCE_S, 2.5 * step_seconds(step))
    series = await prom.query_range(METRIC, start, end, step)
    return extract_intervals(series, window_end=end, gap_tolerance_s=tolerance)


def tag_episodes(episodes, intervals: list[MaintenanceInterval]) -> None:
    """Mark each alert episode that overlapped a window on its node.

    The episode still happened — `ALERTS` is what it is — so this is context
    rather than erasure: the history says "fired, during maintenance", never
    "did not fire".
    """
    by_node: dict[str, list[MaintenanceInterval]] = {}
    for iv in intervals:
        by_node.setdefault(iv.node, []).append(iv)
    for ep in episodes:
        if not ep.node:
            continue
        ep.maintenance = any(
            iv.started_at <= ep.ended_at and iv.ended_at >= ep.started_at
            for iv in by_node.get(ep.node, [])
        )
