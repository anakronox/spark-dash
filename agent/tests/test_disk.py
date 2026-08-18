"""Root filesystem capacity.

Small collector, and every test here is about something it deliberately does
NOT do.
"""

from __future__ import annotations

import os

from spark_dash_agent.collectors.disk import DiskCollector


def test_reports_used_as_total_minus_AVAILABLE(tmp_path):
    """Not total - free. The two differ by the filesystem's reserved blocks,
    and `available` is the basis NodeDiskWarning and NodeDiskLow alert on. A
    card that reads a few points lower than the alert about to fire is worse
    than no card."""
    d = DiskCollector(tmp_path).collect()
    assert d is not None

    st = os.statvfs(tmp_path)
    assert d.total_bytes == st.f_blocks * st.f_frsize
    assert d.available_bytes == st.f_bavail * st.f_frsize
    assert d.used_bytes == d.total_bytes - d.available_bytes


def test_used_pct_uses_the_same_denominator_as_the_alerts():
    from spark_dash_common.models import DiskMetrics

    # 90 used, 10 available -> 90%, which is exactly where NodeDiskWarning sits.
    d = DiskMetrics(total_bytes=100, available_bytes=10, used_bytes=90)
    assert d.used_pct == 90.0


def test_a_missing_host_mount_is_None_not_a_crash(tmp_path, caplog):
    """The agent runs from a compose file that may predate this collector. A
    missing bind mount must degrade to "no number", and must SAY which mount is
    missing — the symptom otherwise is a silently absent reading."""
    collector = DiskCollector(tmp_path / "definitely-not-here")
    assert collector.collect() is None
    assert "node/compose.yaml" in caplog.text


def test_the_reading_is_cached(tmp_path, monkeypatch):
    """Disk fills over hours; the snapshot is built every couple of seconds.

    The TTL is not a micro-optimisation — it bounds how often a filesystem is
    touched at all, which matters because this runs on the snapshot path that Q
    spent an entire section making unblockable."""
    calls = {"n": 0}
    real = os.statvfs

    def counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(os, "statvfs", counting)
    collector = DiskCollector(tmp_path, ttl_s=60.0)
    for _ in range(10):
        collector.collect()
    assert calls["n"] == 1


def test_the_cache_expires(tmp_path, monkeypatch):
    calls = {"n": 0}
    real = os.statvfs

    def counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(os, "statvfs", counting)
    collector = DiskCollector(tmp_path, ttl_s=0.0)
    collector.collect()
    collector.collect()
    assert calls["n"] == 2


def test_it_stats_ONE_path_and_never_walks_mounts(tmp_path, monkeypatch):
    """The design constraint, asserted rather than trusted.

    These nodes mount a NAS. `statvfs` on a stale NFS mount blocks
    uninterruptibly — it does not raise, it hangs — so a collector that
    enumerated filesystems would put an unbounded stall back into snapshot
    collection. Root only, one syscall, one path.
    """
    seen: list[str] = []
    real = os.statvfs

    def recording(path):
        seen.append(str(path))
        return real(path)

    monkeypatch.setattr(os, "statvfs", recording)
    DiskCollector(tmp_path).collect()
    assert seen == [str(tmp_path)]
