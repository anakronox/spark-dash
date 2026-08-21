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
from dataclasses import dataclass

from fastapi import FastAPI, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    ProcessCollector,
    generate_latest,
)
from spark_dash_common.models import NodeSnapshot

from spark_dash_agent.config import Settings
from spark_dash_agent.exporter import CollectionStatsCollector, SnapshotMetricsCollector
from spark_dash_agent.snapshot import SnapshotBuilder

log = logging.getLogger(__name__)

# Shorter than the live-view poll interval, so consecutive polls still see
# fresh data while a burst of requests collapses onto one collection.
_CACHE_TTL_S = 0.75

# How long a reader waits for a refresh before being served the previous
# snapshot instead. See SnapshotCache.get.
_GRACE_S = 0.25


@dataclass(frozen=True)
class CacheStats:
    """What the cache knows about its own collection, for `/metrics`.

    Kept out of NodeSnapshot deliberately: this is the agent describing
    itself, not the node, and the backend has no use for it.
    """

    collect_duration_s: float
    snapshot_age_s: float
    collections: int
    failures: int
    stalled: bool


class SnapshotCache:
    """Serves the newest snapshot it has, and refreshes BEHIND the reader.

    The rule is: a reader never waits for collection. It gets whatever is in
    hand, immediately, and a background thread brings the cache up to date.

    This used to hold one lock across `builder.build()`, which made every
    consumer — Prometheus, the backend's live poll, and `/health` — hostage to
    the slowest thing the node talks to. On 2026-08-18 a router loading a 24B
    model stalled collection past ten seconds; scrape_duration_seconds pinned
    at Prometheus's 10s timeout, the backend's 3s poll timed out through the
    whole recovery tail, and the node vanished from the dashboard for minutes
    while the agent itself was perfectly healthy. Serving a snapshot 0.75s old
    would have been completely fine, and it was already sitting in memory.

    Two locks, and the split is the whole point:

      _state_lock    held for microseconds, guards the fields below. Never
                     held across I/O.
      _refresh_lock  single-flight. One collection at a time, so a burst of
                     readers cannot stack up NVML reads and HTTP round-trips.

    The only caller that still blocks is the very first one after start, when
    there is genuinely nothing to serve.
    """

    def __init__(
        self,
        builder: SnapshotBuilder,
        ttl_s: float = _CACHE_TTL_S,
        grace_s: float = _GRACE_S,
    ) -> None:
        self._builder = builder
        self._ttl_s = ttl_s
        self._grace_s = grace_s

        self._state_lock = threading.Lock()

        self._snapshot: NodeSnapshot | None = None
        self._fetched_at = 0.0
        self._duration_s = 0.0
        self._collections = 0
        self._failures = 0
        self._refreshing = False
        self._done = threading.Event()

    def get(self) -> NodeSnapshot:
        with self._state_lock:
            snapshot = self._snapshot
            age = time.monotonic() - self._fetched_at

        if snapshot is not None and age < self._ttl_s:
            return snapshot

        done = self._start_refresh()

        # Wait a LITTLE for the refresh, then give up and serve what we have.
        #
        # Without this wait, a reader whose snapshot is stale always gets the
        # previous collection — so Prometheus, which is usually the only caller
        # and scrapes every 15s, would record data collected 15s before the
        # scrape it is stamped with. A systematic one-interval lag on all
        # history is too high a price for stall immunity.
        #
        # Collection is ~80ms when the routers answer, so the grace period is
        # long enough that the normal case is FRESH and short enough that a
        # stalled one is served stale inside every consumer's timeout: the
        # backend allows 3s and Prometheus 10s.
        #
        # Cold start waits as long as it takes, having nothing else to offer.
        if done.wait(None if snapshot is None else self._grace_s):
            with self._state_lock:
                if self._snapshot is not None:
                    return self._snapshot

        if snapshot is None:
            raise RuntimeError("initial snapshot collection failed")
        return snapshot

    def stats(self) -> CacheStats:
        with self._state_lock:
            age = time.monotonic() - self._fetched_at if self._snapshot else 0.0
            return CacheStats(
                collect_duration_s=self._duration_s,
                snapshot_age_s=age,
                collections=self._collections,
                failures=self._failures,
                # Refreshing, and what we are serving has aged past the grace
                # period — i.e. readers are now getting stale data. This is the
                # signal that used to be a failed scrape and no longer is,
                # because the agent answers through a stall now.
                stalled=self._refreshing and age >= self._ttl_s + self._grace_s,
            )

    def _start_refresh(self) -> threading.Event:
        """Begin a collection, or join the one already running.

        Single-flight: a burst of readers must not stack up NVML reads and
        rounds of HTTP against the routers. Measured against the old code, a
        20-reader burst produced 21 sequential collections — the stall did not
        merely persist under load, it was amplified by it.
        """
        with self._state_lock:
            if self._refreshing:
                return self._done
            self._refreshing = True
            self._done = threading.Event()
            done = self._done

        threading.Thread(
            target=self._collect, args=(done,), name="snapshot-refresh", daemon=True
        ).start()
        return done

    def _collect(self, done: threading.Event) -> None:
        started = time.monotonic()
        try:
            snapshot = self._builder.build()
        except Exception:  # noqa: BLE001 — a failed refresh keeps the old snapshot
            # Deliberately NOT re-raised. Letting the previous snapshot go stale
            # is a better failure than returning 500 to Prometheus, which then
            # records nothing at all. `failures` keeps it visible.
            log.exception("snapshot collection failed")
            with self._state_lock:
                self._failures += 1
                self._refreshing = False
            done.set()
            return

        elapsed = time.monotonic() - started
        with self._state_lock:
            self._snapshot = snapshot
            self._fetched_at = time.monotonic()
            self._duration_s = elapsed
            self._collections += 1
            self._refreshing = False
        done.set()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    logging.basicConfig(level=settings.log_level.upper())

    builder = SnapshotBuilder(settings)
    cache = SnapshotCache(builder)

    registry = CollectorRegistry()
    registry.register(SnapshotMetricsCollector(cache.get))
    registry.register(CollectionStatsCollector(cache.stats, builder.node_id))
    # WHAT THE AGENT ITSELF COSTS, in the standard shape every other component
    # here already uses: Prometheus, Alertmanager and node_exporter all export
    # `process_resident_memory_bytes` about themselves, and this was the one
    # gap in that set.
    #
    # A DEDICATED REGISTRY MEANS OPTING IN. The default registry carries this
    # automatically; a bare CollectorRegistry does not, which is why the agent
    # was the only process here unable to say what it weighed.
    #
    # Each process measuring ITSELF is what makes the footprint answerable at
    # all. The alternative -- one collector identifying "the monitoring
    # processes" by name -- is the ComfyUI problem again: `python` tells you
    # nothing, and a wrong match would attribute someone's model to monitoring.
    # Registers itself into the registry passed here; there is no separate
    # `.register()` call. It is a no-op off Linux (it reads /proc), which is
    # why this cannot be verified on a dev laptop and is checked on a node.
    ProcessCollector(registry=registry)

    app = FastAPI(
        title="spark-dash-agent",
        summary=f"Per-node metrics agent for {builder.node_id}",
    )

    @app.get("/metrics")
    def metrics() -> Response:
        # The content type MUST match the generator. `generate_latest` emits
        # the plain-text exposition format; advertising OpenMetrics instead
        # makes Prometheus parse strictly and reject the response for a
        # missing trailing "# EOF" marker, which plain text doesn't emit.
        # The symptom is nasty: the agent logs 200 OK for every scrape while
        # Prometheus stores nothing and reports the target down.
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
