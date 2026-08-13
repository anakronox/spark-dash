"""The agent's HTTP surface.

Three endpoints, two consumers:

  /metrics   Prometheus exposition — scraped every ~15s for history.
  /snapshot  JSON — polled by the backend every ~1-2s for the live view.
             JSON rather than exposition format because the backend wants the
             typed model back, not a re-parse of text it just serialized.
  /health    Liveness, for UptimeKuma and container healthchecks.

Both metric paths read through one short-TTL cache, so a Prometheus scrape
landing next to a live poll doesn't hit NVML twice.
"""

from __future__ import annotations

import logging
import threading
import time

from fastapi import FastAPI, Response
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST
from spark_dash_common.models import NodeSnapshot

from spark_dash_agent.config import Settings
from spark_dash_agent.exporter import SnapshotMetricsCollector
from spark_dash_agent.snapshot import SnapshotBuilder

log = logging.getLogger(__name__)

# Shorter than the live-view poll interval, so consecutive polls still see
# fresh data while a burst of requests collapses onto one collection.
_CACHE_TTL_S = 0.75


class SnapshotCache:
    """Caches the latest snapshot briefly and serves it to all readers.

    Collection touches NVML and does HTTP round-trips to the local runtimes, so
    it must not run once per concurrent request.
    """

    def __init__(self, builder: SnapshotBuilder, ttl_s: float = _CACHE_TTL_S) -> None:
        self._builder = builder
        self._ttl_s = ttl_s
        self._lock = threading.Lock()
        self._snapshot: NodeSnapshot | None = None
        self._fetched_at = 0.0

    def get(self) -> NodeSnapshot:
        with self._lock:
            now = time.monotonic()
            if self._snapshot is None or (now - self._fetched_at) >= self._ttl_s:
                self._snapshot = self._builder.build()
                self._fetched_at = now
            return self._snapshot


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    logging.basicConfig(level=settings.log_level.upper())

    cache = SnapshotCache(SnapshotBuilder(settings))

    registry = CollectorRegistry()
    registry.register(SnapshotMetricsCollector(cache.get))

    app = FastAPI(
        title="spark-dash-agent",
        summary=f"Per-node metrics agent for {settings.node_id}",
    )

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/snapshot", response_model=NodeSnapshot)
    def snapshot() -> NodeSnapshot:
        return cache.get()

    @app.get("/health")
    def health() -> dict:
        """Liveness plus a self-assessment.

        Reports degraded (not a failure) when collectors are erroring: the agent
        is still serving, but some data is missing and a watcher should say so
        rather than showing a bare green tick.
        """
        snap = cache.get()
        return {
            "status": "degraded" if snap.errors else "ok",
            "node_id": snap.node_id,
            "failed_collectors": sorted(snap.errors),
        }

    return app


app = create_app()
