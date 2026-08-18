"""The cache that must never make a reader wait on collection.

Every test here is the 2026-08-18 incident in miniature: a router loading a
24B model stalled collection past ten seconds, and because the cache held one
lock across `build()`, Prometheus, the backend's live poll and `/health` all
queued behind it. The agent was healthy the whole time and had a perfectly
good snapshot in memory; it simply refused to hand it over.

So the property under test is not "collection is fast". It is "a reader gets
an answer regardless of how slow collection is".
"""

from __future__ import annotations

import threading
import time

import pytest
from spark_dash_agent.app import SnapshotCache


class FakeBuilder:
    """A builder whose duration and failure mode the test controls."""

    def __init__(self, delay_s: float = 0.0, fail: bool = False) -> None:
        self.delay_s = delay_s
        self.fail = fail
        self.calls = 0
        self.started = threading.Event()
        self._lock = threading.Lock()

    def build(self):
        with self._lock:
            self.calls += 1
        self.started.set()
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.fail:
            raise RuntimeError("router stalled")
        return f"snapshot-{self.calls}"


def test_cold_start_blocks_because_there_is_nothing_else_to_serve():
    """The one caller that legitimately waits. Not a compromise — there is no
    snapshot yet, so there is nothing better to return."""
    cache = SnapshotCache(FakeBuilder(), ttl_s=0.05, grace_s=0.25)
    assert cache.get() == "snapshot-1"


def test_a_slow_refresh_does_not_block_the_reader():
    """THE REGRESSION. Collection takes far longer than any caller's timeout;
    the reader must still get the previous snapshot immediately.

    Before this, `get()` held a lock across `build()`, so this call would have
    taken `delay_s` and blown the backend's 3s poll timeout and Prometheus's
    10s scrape timeout alike."""
    builder = FakeBuilder()
    cache = SnapshotCache(builder, ttl_s=0.01)
    assert cache.get() == "snapshot-1"

    builder.delay_s = 5.0
    time.sleep(0.05)  # let the snapshot go stale

    started = time.monotonic()
    served = cache.get()
    elapsed = time.monotonic() - started

    assert served == "snapshot-1", "must serve the snapshot it already had"
    assert elapsed < 0.5, f"reader waited {elapsed:.2f}s on a 5s collection"


def test_a_burst_of_readers_triggers_ONE_collection():
    """Single-flight. Twenty concurrent readers must not stack up twenty NVML
    reads and twenty rounds of HTTP against the routers."""
    builder = FakeBuilder()
    cache = SnapshotCache(builder, ttl_s=0.01)
    cache.get()
    builder.delay_s = 0.3
    time.sleep(0.05)

    threads = [threading.Thread(target=cache.get) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    builder.started.wait(timeout=2.0)
    time.sleep(0.5)  # let the in-flight refresh finish
    assert builder.calls == 2, f"{builder.calls} collections for one burst"


def test_a_failed_refresh_keeps_the_previous_snapshot():
    """A collection that raises must not blank the cache. Serving a stale
    snapshot beats returning 500 to Prometheus, which records nothing at all."""
    builder = FakeBuilder()
    cache = SnapshotCache(builder, ttl_s=0.01)
    assert cache.get() == "snapshot-1"

    builder.fail = True
    time.sleep(0.05)
    cache.get()
    time.sleep(0.3)

    assert cache.get() == "snapshot-1"
    assert cache.stats().failures >= 1


def test_a_fresh_snapshot_is_served_without_refreshing():
    """The TTL still does its original job: a scrape landing next to a live
    poll must not collect twice."""
    builder = FakeBuilder()
    cache = SnapshotCache(builder, ttl_s=10.0)
    cache.get()
    for _ in range(5):
        cache.get()
    assert builder.calls == 1


def test_stats_report_duration_and_age():
    """Q4. The agent had no view of its own collection time, which is why the
    incident was invisible until a scrape actually failed."""
    builder = FakeBuilder(delay_s=0.2)
    cache = SnapshotCache(builder, ttl_s=10.0)
    cache.get()

    stats = cache.stats()
    assert stats.collect_duration_s == pytest.approx(0.2, abs=0.15)
    assert stats.collections == 1
    assert stats.failures == 0
    assert stats.snapshot_age_s >= 0.0


def test_a_fast_refresh_is_served_FRESH_not_stale():
    """Stale-while-revalidate must not become stale-always.

    Prometheus is usually the only caller and scrapes every 15s, so if a
    stale-but-present snapshot were always returned immediately, every sample
    in history would be data collected 15s before the timestamp it is stored
    under. The grace period exists to keep the normal case exact: collection
    takes ~80ms in practice, well inside it."""
    builder = FakeBuilder(delay_s=0.02)
    cache = SnapshotCache(builder, ttl_s=0.01, grace_s=0.5)
    assert cache.get() == "snapshot-1"
    time.sleep(0.05)

    assert cache.get() == "snapshot-2", "a fast refresh should have been waited for"


def test_the_grace_period_is_the_ceiling_on_a_reader_s_wait():
    """The bound that matters: however long collection takes, a reader waits
    at most the grace period. The backend allows 3s and Prometheus 10s, so
    0.25s leaves both untouched by a stall that used to blow through them."""
    builder = FakeBuilder()
    cache = SnapshotCache(builder, ttl_s=0.01, grace_s=0.2)
    cache.get()
    builder.delay_s = 5.0
    time.sleep(0.05)

    started = time.monotonic()
    served = cache.get()
    elapsed = time.monotonic() - started

    assert served == "snapshot-1"
    assert 0.15 < elapsed < 0.6, f"waited {elapsed:.2f}s against a 0.2s grace period"
