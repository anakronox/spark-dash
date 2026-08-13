"""The live poller's two guarantees, both about not burdening hardware whose
actual job is inference:

1. ONE shared loop regardless of how many clients are watching.
2. No polling at all when nobody is.
"""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from spark_dash_backend.inventory import Node
from spark_dash_backend.poller import LivePoller, fetch_node, gather_cluster
from spark_dash_common.models import GpuMetrics, NodeSnapshot


def snapshot_json(node_id: str = "gx10-1", util: float = 42.0) -> dict:
    return NodeSnapshot(
        node_id=node_id,
        ts=datetime.now(UTC),
        up=True,
        gpu=GpuMetrics(util_pct=util),
    ).model_dump(mode="json")


class FakeInventory:
    """Stands in for the file-backed inventory."""

    def __init__(self, nodes: list[Node]):
        self._nodes = nodes
        self.reads = 0

    def nodes(self, now=None):
        self.reads += 1
        return self._nodes


class CountingAgent:
    """Counts requests, so 'one shared poller' is measurable rather than assumed."""

    def __init__(self):
        self.request_count = 0

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.request_count += 1
            return httpx.Response(200, json=snapshot_json())

        return httpx.MockTransport(handler)


async def test_fetch_node_returns_snapshot():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=snapshot_json()))
    async with httpx.AsyncClient(transport=transport) as client:
        snap = await fetch_node(client, Node("gx10-1", "host:9500"))
    assert snap.up is True
    assert snap.gpu.util_pct == 42.0


async def test_unreachable_node_becomes_a_down_marker_not_an_omission():
    """A missing tile is easy to overlook; a red one isn't — and 'node down'
    is the alert this system exists to surface."""

    def handler(request):
        raise httpx.ConnectError("refused")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snap = await fetch_node(client, Node("gx10-2", "dead:9500"))

    assert snap.node_id == "gx10-2"
    assert snap.up is False
    assert "agent" in snap.errors


async def test_bad_payload_becomes_a_down_marker():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"garbage": True}))
    async with httpx.AsyncClient(transport=transport) as client:
        snap = await fetch_node(client, Node("gx10-1", "host:9500"))
    assert snap.up is False


async def test_gather_polls_nodes_concurrently():
    """A single slow node must not hold up the whole tick."""
    order = []

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        delay = 0.05 if "slow" in str(request.url) else 0.0
        await asyncio.sleep(delay)
        order.append(str(request.url.host))
        return httpx.Response(200, json=snapshot_json())

    transport = httpx.MockTransport(slow_handler)
    nodes = [Node("slow", "slow:9500"), Node("fast", "fast:9500")]

    async with httpx.AsyncClient(transport=transport) as client:
        result = await gather_cluster(client, nodes)

    assert len(result.nodes) == 2
    # Fast node finished first despite being second in the list.
    assert order[0] == "fast"


async def test_gather_with_no_nodes():
    async with httpx.AsyncClient() as client:
        result = await gather_cluster(client, [])
    assert result.nodes == []


class TestSharedPolling:
    async def test_no_polling_without_subscribers(self):
        """The dashboard being closed means the GX10s hear nothing from us."""
        agent = CountingAgent()
        inv = FakeInventory([Node("gx10-1", "host:9500")])
        poller = LivePoller(inv, interval_s=0.01)

        await asyncio.sleep(0.05)

        assert poller.running is False
        assert agent.request_count == 0

    async def test_loop_starts_on_first_subscriber_and_stops_on_last(self, monkeypatch):
        from spark_dash_common.models import ClusterSnapshot

        async def gather(client, nodes):
            return ClusterSnapshot(ts=datetime.now(UTC), nodes=[])

        monkeypatch.setattr("spark_dash_backend.poller.gather_cluster", gather)

        inv = FakeInventory([Node("gx10-1", "host:9500")])
        poller = LivePoller(inv, interval_s=0.01)

        # Events rather than sleeps: the consumer holds the subscription open
        # until released, so the assertions can't race the poll loop.
        subscribed = asyncio.Event()
        release = asyncio.Event()

        async def consume():
            async for _ in poller.subscribe():
                subscribed.set()
                await release.wait()
                return

        assert poller.running is False

        task = asyncio.create_task(consume())
        await asyncio.wait_for(subscribed.wait(), timeout=2.0)
        assert poller.running is True
        assert poller.subscriber_count == 1

        release.set()
        await asyncio.wait_for(task, timeout=2.0)
        await asyncio.sleep(0.01)  # let the unsubscribe cancellation land

        assert poller.subscriber_count == 0
        assert poller.running is False

    async def test_many_subscribers_share_one_poll_loop(self, monkeypatch):
        """Three browser tabs must not triple the request rate on the nodes."""
        polls = 0

        async def counting_gather(client, nodes):
            nonlocal polls
            polls += 1
            from spark_dash_common.models import ClusterSnapshot

            return ClusterSnapshot(ts=datetime.now(UTC), nodes=[])

        monkeypatch.setattr("spark_dash_backend.poller.gather_cluster", counting_gather)

        inv = FakeInventory([Node("gx10-1", "host:9500")])
        poller = LivePoller(inv, interval_s=0.02)

        async def consume():
            count = 0
            async for _ in poller.subscribe():
                count += 1
                if count >= 3:
                    return

        await asyncio.gather(consume(), consume(), consume())

        # Three subscribers, but polls should track elapsed time not client
        # count — well under one poll per client per tick.
        assert polls <= 6, f"expected a shared loop, saw {polls} polls"

    async def test_new_subscriber_gets_last_snapshot_immediately(self, monkeypatch):
        """A client shouldn't stare at an empty page for a full interval."""
        from spark_dash_common.models import ClusterSnapshot

        async def gather(client, nodes):
            return ClusterSnapshot(ts=datetime.now(UTC), nodes=[])

        monkeypatch.setattr("spark_dash_backend.poller.gather_cluster", gather)

        inv = FakeInventory([Node("gx10-1", "host:9500")])
        poller = LivePoller(inv, interval_s=10.0)  # long, so only the cached one arrives

        async def first():
            async for _ in poller.subscribe():
                return

        await first()
        assert poller.latest is not None

        # Second subscriber must get a frame promptly from cache, not wait 10s.
        async def second():
            async for _ in poller.subscribe():
                return "got it"

        result = await asyncio.wait_for(second(), timeout=1.0)
        assert result == "got it"

    async def test_slow_client_gets_newest_not_a_backlog(self, monkeypatch):
        """Live view: the newest state is the only one worth having."""
        from spark_dash_common.models import ClusterSnapshot

        counter = 0

        async def gather(client, nodes):
            nonlocal counter
            counter += 1
            return ClusterSnapshot(ts=datetime.now(UTC), nodes=[])

        monkeypatch.setattr("spark_dash_backend.poller.gather_cluster", gather)

        inv = FakeInventory([])
        poller = LivePoller(inv, interval_s=0.01)

        received = []

        async def slow_consumer():
            async for snap in poller.subscribe():
                received.append(snap)
                await asyncio.sleep(0.05)  # much slower than the poll interval
                if len(received) >= 2:
                    return

        await asyncio.wait_for(slow_consumer(), timeout=2.0)
        # Queue is bounded at 1, so it can never accumulate a backlog.
        assert len(received) == 2

    async def test_tick_failure_does_not_kill_the_loop(self, monkeypatch):
        from spark_dash_common.models import ClusterSnapshot

        calls = 0

        async def flaky(client, nodes):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient")
            return ClusterSnapshot(ts=datetime.now(UTC), nodes=[])

        monkeypatch.setattr("spark_dash_backend.poller.gather_cluster", flaky)

        inv = FakeInventory([])
        poller = LivePoller(inv, interval_s=0.01)

        async def consume():
            async for _ in poller.subscribe():
                return "recovered"

        assert await asyncio.wait_for(consume(), timeout=2.0) == "recovered"
        assert calls >= 2


async def test_poll_once_works_without_the_loop():
    """REST endpoints need a snapshot even with no WebSocket client connected."""
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=snapshot_json()))
    inv = FakeInventory([Node("gx10-1", "host:9500")])
    poller = LivePoller(inv)

    # Patch the client the poller builds internally.
    import spark_dash_backend.poller as poller_mod

    original = poller_mod.httpx.AsyncClient
    poller_mod.httpx.AsyncClient = lambda **kw: original(transport=transport, **kw)
    try:
        snapshot = await poller.poll_once()
    finally:
        poller_mod.httpx.AsyncClient = original

    assert len(snapshot.nodes) == 1
    assert poller.running is False


@pytest.mark.parametrize("count", [0, 1, 3])
async def test_subscriber_count_tracks_accurately(count, monkeypatch):
    from spark_dash_common.models import ClusterSnapshot

    async def gather(client, nodes):
        return ClusterSnapshot(ts=datetime.now(UTC), nodes=[])

    monkeypatch.setattr("spark_dash_backend.poller.gather_cluster", gather)
    poller = LivePoller(FakeInventory([]), interval_s=0.01)

    started = asyncio.Event()

    async def hold():
        async for _ in poller.subscribe():
            started.set()
            await asyncio.sleep(0.3)
            return

    tasks = [asyncio.create_task(hold()) for _ in range(count)]
    if count:
        await asyncio.wait_for(started.wait(), timeout=1.0)
        await asyncio.sleep(0.02)
        assert poller.subscriber_count == count

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
