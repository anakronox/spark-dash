"""The live-view fast path: poll every node's agent and fan out to subscribers.

Two properties this exists to guarantee, both about not being a burden on
hardware whose actual job is inference:

1. ONE poller, not one per connection. Three browser tabs must not triple the
   request rate against the GX10s.
2. It runs only while someone is subscribed. With the dashboard closed, the
   nodes see nothing from us. Prometheus keeps its own slower scrape going
   regardless, so history has no gaps either way.

Deliberately bypasses Prometheus. Its 15s scrape interval is right for trends
and useless for the thing that made nvtop worth SSHing into — sub-2s response
to what's happening now.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
from spark_dash_common.models import ClusterSnapshot, NodeSnapshot

from spark_dash_backend.inventory import Inventory, Node

log = logging.getLogger(__name__)


async def fetch_node(client: httpx.AsyncClient, node: Node) -> NodeSnapshot:
    """Fetch one node's snapshot, or synthesize a down marker.

    An unreachable node returns a snapshot with `up=False` rather than being
    omitted. A missing tile is easy to overlook; a red one isn't — and "node
    down" is the alert this whole system exists to surface.
    """
    try:
        resp = await client.get(node.snapshot_url)
        resp.raise_for_status()
        return NodeSnapshot.model_validate(resp.json())
    except Exception as exc:  # noqa: BLE001 — any failure means "can't see it"
        log.debug("snapshot fetch failed for %s", node.node_id, exc_info=True)
        return NodeSnapshot(
            node_id=node.node_id,
            ts=datetime.now(UTC),
            up=False,
            errors={"agent": f"{type(exc).__name__}: {exc}"},
        )


async def gather_cluster(
    client: httpx.AsyncClient, nodes: list[Node]
) -> ClusterSnapshot:
    """Poll every node concurrently.

    Concurrent because a single slow node must not delay the whole tick —
    sequential polling would make the refresh rate hostage to the worst node.
    """
    if not nodes:
        return ClusterSnapshot(ts=datetime.now(UTC), nodes=[])

    snapshots = await asyncio.gather(*(fetch_node(client, n) for n in nodes))
    return ClusterSnapshot(ts=datetime.now(UTC), nodes=list(snapshots))


class LivePoller:
    """Shared poll loop with reference-counted subscribers."""

    def __init__(
        self,
        inventory: Inventory,
        *,
        interval_s: float = 2.0,
        timeout_s: float = 3.0,
    ) -> None:
        self._inventory = inventory
        self._interval_s = interval_s
        self._timeout_s = timeout_s

        self._subscribers: set[asyncio.Queue[ClusterSnapshot]] = set()
        self._task: asyncio.Task | None = None
        self._latest: ClusterSnapshot | None = None
        self._lock = asyncio.Lock()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def latest(self) -> ClusterSnapshot | None:
        """Most recent snapshot, if the loop has produced one."""
        return self._latest

    async def subscribe(self) -> AsyncIterator[ClusterSnapshot]:
        """Yield snapshots until the consumer stops iterating.

        Starts the shared loop on the first subscriber and stops it when the
        last one leaves.
        """
        # maxsize=1 with drop-oldest: a slow client should see the *newest*
        # state when it catches up, not work through a backlog of stale frames.
        queue: asyncio.Queue[ClusterSnapshot] = asyncio.Queue(maxsize=1)

        async with self._lock:
            self._subscribers.add(queue)
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._run())
                log.info("live poller started (interval %.1fs)", self._interval_s)

        # Hand over the last known state immediately so a new client renders
        # at once instead of staring at an empty page for a full interval.
        if self._latest is not None:
            queue.put_nowait(self._latest)

        try:
            while True:
                yield await queue.get()
        finally:
            await self._unsubscribe(queue)

    async def _unsubscribe(self, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers.discard(queue)
            if not self._subscribers and self._task is not None:
                self._task.cancel()
                self._task = None
                log.info("live poller stopped (no subscribers)")

    async def _run(self) -> None:
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            while True:
                started = asyncio.get_running_loop().time()
                try:
                    snapshot = await gather_cluster(client, self._inventory.nodes())
                    self._latest = snapshot
                    self._publish(snapshot)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — never let one bad tick kill the loop
                    log.exception("live poll tick failed")

                # Subtract the work already done, so the cadence is the
                # interval rather than interval-plus-poll-time.
                elapsed = asyncio.get_running_loop().time() - started
                await asyncio.sleep(max(0.0, self._interval_s - elapsed))

    def _publish(self, snapshot: ClusterSnapshot) -> None:
        for queue in self._subscribers:
            if queue.full():
                # Drop the stale frame this client hasn't read yet. Live view:
                # the newest state is the only one worth having.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(snapshot)

    async def poll_once(self) -> ClusterSnapshot:
        """One-shot poll for REST callers, independent of the loop."""
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            snapshot = await gather_cluster(client, self._inventory.nodes())
        self._latest = snapshot
        return snapshot
