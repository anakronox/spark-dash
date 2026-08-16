"""The backend's HTTP surface.

Two distinct jobs, deliberately kept apart:

  REST       history and trends, answered from Prometheus.
  WebSocket  live state, polled straight from the agents at ~2s.

The split exists because they have different truths. Prometheus is right about
"what happened"; only a direct poll is fresh enough for "what's happening", and
mixing them would make the live view as laggy as the scrape interval.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from spark_dash_common.models import ClusterSnapshot

from spark_dash_backend.alert_history import fetch_episodes, summarise
from spark_dash_backend.alerts import AlertmanagerClient
from spark_dash_backend.cluster import ClusterConfigError, load_cluster
from spark_dash_backend.config import Settings
from spark_dash_backend.inventory import Inventory
from spark_dash_backend.poller import LivePoller
from spark_dash_backend.prometheus import HISTORY_QUERIES, PrometheusClient, PrometheusError
from spark_dash_backend.timeline import fetch_events

log = logging.getLogger(__name__)

# How stale the cached cluster view may be before /health polls afresh. Short
# enough that a health check reflects reality, long enough that a monitoring
# system hitting it every few seconds doesn't drive the poll rate.
HEALTH_SNAPSHOT_MAX_AGE_S = 30.0

# Longest silence the dashboard will create. Alertmanager itself has no such
# limit; this is a deliberate product decision. The failure mode of silencing
# is forgetting, and a week-long mute set during a five-minute experiment is
# indistinguishable from a real outage nobody is watching. Anything that needs
# permanent silence should have its target removed from configuration.
MAX_SILENCE_HOURS = 24.0


def _age_seconds(ts: datetime) -> float:
    return (datetime.now(UTC) - ts).total_seconds()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    logging.basicConfig(level=settings.log_level.upper())

    inventory = Inventory(
        nodes_env=settings.spark_nodes,
        targets_file=settings.agent_targets_file,
        prometheus_targets_dir=settings.prometheus_targets_dir,
        agent_port=settings.agent_port,
        node_exporter_port=settings.node_exporter_port,
        ttl_s=settings.inventory_ttl_s,
    )
    poller = LivePoller(
        inventory,
        interval_s=settings.live_poll_interval_s,
        timeout_s=settings.agent_timeout_s,
    )
    prom = PrometheusClient(settings.prometheus_url, timeout_s=settings.prometheus_timeout_s)
    alertmanager = AlertmanagerClient(
        settings.alertmanager_url, timeout_s=settings.alertmanager_timeout_s
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nodes = inventory.nodes()
        log.info(
            "backend starting; %d node(s) from %s: %s",
            len(nodes),
            inventory.source,
            ", ".join(n.node_id for n in nodes) or "(none)",
        )
        # Render Prometheus's targets from the same list we poll, so the two
        # views of the cluster cannot disagree.
        inventory.sync_prometheus_targets()
        yield

    app = FastAPI(title="spark-dash", summary="GB10 inference cluster dashboard", lifespan=lifespan)
    app.state.poller = poller
    app.state.inventory = inventory

    # ---------------------------------------------------------------- live

    @app.websocket("/ws/live")
    async def ws_live(websocket: WebSocket) -> None:
        """Push a full cluster snapshot every tick.

        Full snapshots rather than deltas: a few KB at 0.5Hz is nothing on a
        LAN, and it keeps both ends stateless — no resync after a reconnect,
        no delta-application bugs.
        """
        await websocket.accept()
        try:
            async for snapshot in poller.subscribe():
                await websocket.send_text(snapshot.model_dump_json())
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001 — a dead client must not kill the poller
            log.debug("websocket closed unexpectedly", exc_info=True)

    # ---------------------------------------------------------------- REST

    @app.get("/api/nodes")
    async def api_nodes() -> dict:
        """Inventory plus current liveness."""
        snapshot = poller.latest or await poller.poll_once()
        by_id = {n.node_id: n for n in snapshot.nodes}
        return {
            "nodes": [
                {
                    "node_id": node.node_id,
                    "address": node.address,
                    "host": node.host,
                    "group": node.group,
                    "up": by_id[node.node_id].up if node.node_id in by_id else False,
                    "health": (
                        by_id[node.node_id].health.value if node.node_id in by_id else "critical"
                    ),
                }
                for node in inventory.nodes()
            ]
        }

    @app.get("/api/cluster/summary")
    async def api_cluster_summary() -> dict:
        """Headline numbers, aggregated per group.

        Memory sums WITHIN a group and never across groups. Clustered nodes
        pool their memory, so a model can span them and their combined free
        space is real capacity. Unclustered nodes can't do that, so a
        cluster-wide total would describe capacity that doesn't exist and
        answer "can I load this?" with a confident yes when the answer is no.
        """
        snapshot: ClusterSnapshot = poller.latest or await poller.poll_once()
        by_id = {n.node_id: n for n in snapshot.nodes}

        groups: dict[str, dict] = {}
        for node in inventory.nodes():
            g = groups.setdefault(
                node.group_key,
                {
                    "group": node.group,
                    "standalone": node.standalone,
                    "nodes": [],
                    "nodes_up": 0,
                    "memory_total_bytes": 0,
                    "memory_used_bytes": 0,
                    "tokens_per_second": 0.0,
                },
            )
            g["nodes"].append(node.node_id)

            snap = by_id.get(node.node_id)
            if snap is None:
                continue
            if snap.up:
                g["nodes_up"] += 1
            if snap.memory:
                g["memory_total_bytes"] += snap.memory.total_bytes
                g["memory_used_bytes"] += snap.memory.used_bytes
            g["tokens_per_second"] += snap.total_tokens_per_sec

        for g in groups.values():
            g["memory_free_bytes"] = max(0, g["memory_total_bytes"] - g["memory_used_bytes"])

        # The largest block a single model could actually occupy: the best any
        # one group can offer, not the sum of all of them.
        largest = max(
            (g for g in groups.values() if g["nodes_up"]),
            key=lambda g: g["memory_free_bytes"],
            default=None,
        )

        return {
            "ts": snapshot.ts,
            "nodes_total": len(inventory.nodes()),
            "nodes_up": snapshot.nodes_up,
            "tokens_per_second": snapshot.total_tokens_per_sec,
            "groups": [
                {"key": key, **value} for key, value in sorted(groups.items())
            ],
            "largest_free_block_bytes": largest["memory_free_bytes"] if largest else 0,
            "largest_free_block_group": (
                (largest["group"] or largest["nodes"][0]) if largest else None
            ),
        }

    @app.get("/api/models")
    async def api_models() -> dict:
        """What's running where: node x runtime x model x state."""
        snapshot = poller.latest or await poller.poll_once()
        rows = []

        for node in snapshot.nodes:
            for router in node.runtimes.llama_cpp:
                for model in router.models:
                    rows.append(
                        {
                            "node": node.node_id,
                            "runtime": "llama.cpp",
                            "router": router.name or router.endpoint,
                            "model": model.name,
                            "state": model.state.value,
                            "raw_status": model.raw_status,
                            "tokens_per_second": model.tokens_per_sec,
                            "kv_cache_pct": model.kv_cache_pct,
                            "requests_running": model.requests_running,
                            "requests_waiting": model.requests_waiting,
                        }
                    )
            for instance in node.runtimes.vllm:
                rows.append(
                    {
                        "node": node.node_id,
                        "runtime": "vllm",
                        "router": None,
                        "model": instance.model,
                        "state": "active",
                        "raw_status": "",
                        "tokens_per_second": instance.tokens_per_sec,
                        "kv_cache_pct": instance.kv_cache_pct,
                        "requests_running": instance.requests_running,
                        "requests_waiting": instance.requests_waiting,
                    }
                )

        return {"models": rows}

    @app.get("/api/history")
    async def api_history(
        metric: str = Query(..., description=f"One of: {', '.join(sorted(HISTORY_QUERIES))}"),
        minutes: int = Query(60, ge=1, le=60 * 24 * 30),
        step: str = Query("60s"),
        node: str | None = None,
    ) -> dict:
        """Range query for the trend charts.

        `metric` is a key into a fixed query map rather than raw PromQL —
        callers pick from a menu instead of composing queries, so a
        frontend bug can't turn into an arbitrary expensive Prometheus
        query.
        """
        expr = HISTORY_QUERIES.get(metric)
        if expr is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown metric {metric!r}; valid: {sorted(HISTORY_QUERIES)}",
            )
        if node:
            expr = f'{expr}{{node="{_sanitize_label(node)}"}}'

        end = time.time()
        start = end - minutes * 60
        try:
            series = await prom.query_range(expr, start, end, step)
        except PrometheusError as exc:
            raise HTTPException(status_code=503, detail=f"prometheus: {exc}") from exc

        return {
            "metric": metric,
            "series": [
                {"node": s.node, "labels": s.labels, "points": s.points} for s in series
            ],
        }

    @app.get("/api/models/timeline")
    async def api_model_timeline(
        minutes: int = Query(360, ge=5, le=60 * 24 * 7),
        step: str = Query("60s"),
    ) -> dict:
        """When models loaded, slept and unloaded.

        Reconstructed from Prometheus rather than stored: the agent's one-hot
        state metric already records every transition, so history reaches back
        as far as retention does instead of starting when this shipped.
        """
        end = time.time()
        start = end - minutes * 60
        try:
            events = await fetch_events(prom, start=start, end=end, step=step)
        except PrometheusError as exc:
            raise HTTPException(status_code=503, detail=f"prometheus: {exc}") from exc

        return {
            "window_minutes": minutes,
            "events": [e.as_dict() for e in events],
            # Transitions that cost a user latency — the number worth watching
            # if requests feel slow.
            "cold_starts": sum(1 for e in events if e.cold),
        }

    @app.get("/api/agent-config")
    async def api_agent_config(node: str) -> dict:
        """What a node should be polling.

        This is what makes the per-node stack identical everywhere: the agent
        asks what it serves rather than carrying it in its own `.env`, so the
        same image and the same compose file deploy unchanged to every GX10.

        Polling stays ON the node deliberately. The obvious alternative — the
        backend scraping routers itself — cannot work: deciding whether it is
        safe to scrape a model needs NVML per-process utilization, which only
        the agent has, and getting that wrong pins models in memory.

        Returns empty runtimes for an unknown node rather than 404-ing. A node
        that is running but not yet in the cluster file is a normal state
        during a rollout, and the agent should degrade to "no runtimes" rather
        than treat it as an error and retry-storm.
        """
        try:
            cluster = load_cluster(settings.cluster_config)
        except ClusterConfigError as exc:
            # Loud, not silent: a typo here would otherwise leave every node
            # reporting no models with nothing explaining why.
            raise HTTPException(status_code=500, detail=f"cluster config: {exc}") from exc

        runtimes = cluster.get(node)
        return {
            "node": node,
            "configured": runtimes is not None,
            "runtimes": (runtimes.as_dict() if runtimes else {"llama_routers": [], "vllm": []}),
        }

    @app.get("/api/alerts/silences")
    async def api_silences() -> dict:
        """Active silences.

        Surfaced rather than hidden: a muted alert nobody can see is a way to
        hide problems from yourself, so anything currently silenced has to be
        visible in the same place the alerts are.
        """
        return {"silences": await alertmanager.silences()}

    @app.post("/api/alerts/silence")
    async def api_create_silence(body: dict) -> dict:
        """Silence an alert for a bounded period.

        The one write in an otherwise read-only dashboard. Allowed because it
        is a far narrower primitive than the writes that were ruled out — a
        silence cannot repoint an agent, load a model or touch a process — and
        because an alert you cannot clear is one you learn to ignore.

        Scoped to the labels the caller sends, so silencing a torn-down stack
        on one node does not mute the same alert everywhere. `hours` is capped:
        a mute that outlives the person's memory of setting it is how a real
        failure goes unnoticed, and a permanently unwanted alert should have
        its target removed from configuration instead.
        """
        labels = body.get("labels") or {}
        if not labels:
            raise HTTPException(status_code=400, detail="labels are required")

        hours = float(body.get("hours", 4))
        if not 0 < hours <= MAX_SILENCE_HOURS:
            raise HTTPException(
                status_code=400,
                detail=f"hours must be between 0 and {MAX_SILENCE_HOURS}",
            )

        matchers = [
            {"name": k, "value": str(v), "isRegex": False, "isEqual": True}
            for k, v in labels.items()
        ]
        try:
            silence_id = await alertmanager.create_silence(
                matchers,
                hours=hours,
                comment=str(body.get("comment") or "Silenced from the dashboard"),
            )
        except Exception as exc:  # noqa: BLE001 — surfaced to the caller
            raise HTTPException(status_code=502, detail=f"alertmanager: {exc}") from exc

        return {"silence_id": silence_id}

    @app.delete("/api/alerts/silence/{silence_id}")
    async def api_expire_silence(silence_id: str) -> dict:
        """End a silence early — the undo for a mute applied by mistake."""
        try:
            await alertmanager.expire_silence(silence_id)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"alertmanager: {exc}") from exc
        return {"expired": silence_id}

    @app.get("/api/alerts/history")
    async def api_alert_history(
        minutes: int = Query(60 * 24 * 7, ge=5, le=60 * 24 * 180),
        step: str = Query("60s"),
    ) -> dict:
        """What has fired, and what only ever went pending.

        Read from Prometheus's own `ALERTS` series, not from Alertmanager,
        which keeps no useful history — that gap is the reason this exists.

        `pending_only` is the number worth reading first. An alert that keeps
        going pending and never fires means its rule is mistuned rather than
        its condition being rare, and that state is invisible in every other
        view including Alertmanager's own.
        """
        end = time.time()
        start = end - minutes * 60
        try:
            episodes = await fetch_episodes(prom, start=start, end=end, step=step)
        except PrometheusError as exc:
            raise HTTPException(status_code=503, detail=f"prometheus: {exc}") from exc

        return {
            "window_minutes": minutes,
            "summary": summarise(episodes),
            "episodes": [e.as_dict() for e in episodes],
        }

    @app.get("/api/alerts")
    async def api_alerts() -> dict:
        """Firing alerts, for the dashboard banner.

        `available` is reported separately from an empty list: "nothing is
        wrong" and "we can't tell whether anything is wrong" must not look
        identical, since only one of them is reassuring.
        """
        reachable = await alertmanager.reachable()
        alerts = await alertmanager.firing() if reachable else []
        return {
            "available": reachable,
            "alerts": [a.as_dict() for a in alerts],
            "critical": sum(1 for a in alerts if a.severity == "critical"),
            "warning": sum(1 for a in alerts if a.severity == "warning"),
        }

    # -------------------------------------------------------------- health

    @app.get("/health")
    async def health() -> dict:
        """Liveness plus self-assessment, for UptimeKuma.

        Reports `degraded` rather than a bare 200 when Prometheus is
        unreachable or no node is answering — a backend that's running but
        blind should not pass a naive uptime check.
        """
        nodes = inventory.nodes()
        # Queried once each: calling reachable() per use could report
        # differently within a single response.
        prom_ok = await prom.healthy()
        alerts_ok = await alertmanager.reachable()

        # Poll if we have no reasonably fresh view of the cluster. The live
        # poller only runs while a dashboard is open, so without this the
        # check would report "ok" having never contacted a node — exactly the
        # blind-but-green state this endpoint exists to catch.
        snapshot = poller.latest
        if snapshot is None or _age_seconds(snapshot.ts) > HEALTH_SNAPSHOT_MAX_AGE_S:
            try:
                snapshot = await poller.poll_once()
            except Exception:  # noqa: BLE001 — report the failure, don't raise
                log.exception("health check poll failed")
                snapshot = None

        nodes_up = snapshot.nodes_up if snapshot else None

        problems = []
        if not prom_ok:
            problems.append("prometheus unreachable")
        if not alerts_ok:
            # Worth flagging: with Alertmanager down, nothing would notify you
            # of anything, including that Alertmanager is down.
            problems.append("alertmanager unreachable")
        if not nodes:
            problems.append("inventory empty")
        if nodes_up == 0 and nodes:
            problems.append("no nodes reachable")
        elif nodes_up is not None and nodes and nodes_up < len(nodes):
            # Partial outage is degraded, not ok: on a 3-node cluster losing
            # one node is exactly what this should surface.
            problems.append(f"{len(nodes) - nodes_up} of {len(nodes)} node(s) unreachable")
        elif nodes_up is None:
            problems.append("could not reach any node to check")

        return {
            "status": "degraded" if problems else "ok",
            "problems": problems,
            "prometheus": "ok" if prom_ok else "unreachable",
            "alertmanager": "ok" if alerts_ok else "unreachable",
            "nodes_configured": len(nodes),
            "nodes_up": nodes_up,
            # Distinct builds across the cluster, so a node left behind on an
            # older agent is visible from a plain curl rather than only in the
            # UI. Uniform is the boring case and shows one entry.
            "agent_versions": sorted(
                {n.agent_version for n in (snapshot.nodes if snapshot else []) if n.up}
            ),
            "live_poller_running": poller.running,
            "live_subscribers": poller.subscriber_count,
        }

    _mount_frontend(app, settings)
    return app


def _sanitize_label(value: str) -> str:
    """Escape a label value for interpolation into PromQL.

    Node ids come from our own inventory rather than user input, but the query
    string reaches this endpoint from the browser — so it gets escaped anyway.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _mount_frontend(app: FastAPI, settings: Settings) -> None:
    """Serve the built Svelte assets, when present.

    Same origin as the API, so there's no CORS config and the WebSocket needs
    no separate host. Absent in development, where Vite serves them instead.
    """
    static_dir = settings.static_dir
    if not static_dir.is_dir():
        log.info("no static assets at %s; API-only mode", static_dir)
        return

    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")


app = create_app()
