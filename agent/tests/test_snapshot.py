"""Snapshot assembly: partial failure must degrade, not collapse.

Also covers host-procfs redirection — a container reading its own /proc reports
the container's memory view rather than the machine's, which is a bug that would
otherwise look completely plausible in production.
"""

from pathlib import Path

import psutil
import pytest
from spark_dash_agent.config import Settings
from spark_dash_agent.snapshot import SnapshotBuilder, _point_psutil_at_host_proc
from spark_dash_common.models import HealthState


@pytest.fixture(autouse=True)
def restore_procfs_path():
    original = getattr(psutil, "PROCFS_PATH", None)
    yield
    if original is not None:
        psutil.PROCFS_PATH = original


def test_psi_collector_reads_from_configured_proc_path(tmp_path):
    """The PSI path must follow proc_path, not stay pinned to /proc."""
    pressure = tmp_path / "pressure"
    pressure.mkdir()
    (pressure / "memory").write_text(
        "some avg10=30.00 avg60=1.00 avg300=0.00 total=1\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    )

    builder = SnapshotBuilder(Settings(node_id="n1", proc_path=tmp_path))
    metrics = builder._psi.collect()

    assert metrics is not None
    assert metrics.some_avg10 == 30.0


@pytest.mark.skipif(not hasattr(psutil, "PROCFS_PATH"), reason="Linux-only knob")
def test_psutil_is_redirected_to_host_proc(tmp_path):
    _point_psutil_at_host_proc(tmp_path)
    assert str(tmp_path) == psutil.PROCFS_PATH


@pytest.mark.skipif(not hasattr(psutil, "PROCFS_PATH"), reason="Linux-only knob")
def test_default_proc_path_is_left_alone():
    """No redirect when running directly on the host."""
    before = psutil.PROCFS_PATH
    _point_psutil_at_host_proc(Path("/proc"))
    assert before == psutil.PROCFS_PATH


@pytest.mark.skipif(not hasattr(psutil, "PROCFS_PATH"), reason="Linux-only knob")
def test_nonexistent_proc_path_is_ignored():
    """A bad mount must not silently redirect psutil at nothing."""
    before = psutil.PROCFS_PATH
    _point_psutil_at_host_proc(Path("/definitely/not/here"))
    assert before == psutil.PROCFS_PATH


def test_snapshot_survives_missing_gpu():
    """A dev box or CI has no NVML; the agent must still produce a snapshot."""
    builder = SnapshotBuilder(Settings(node_id="gx10-1"))
    snap = builder.build()

    assert snap.node_id == "gx10-1"
    assert snap.up is True
    # Memory and CPU work anywhere, so they should be present regardless.
    assert snap.memory is not None
    assert snap.cpu is not None


def test_failed_collectors_are_recorded_not_raised():
    builder = SnapshotBuilder(Settings(node_id="n1"))
    snap = builder.build()
    # On a machine with no GPU this records an error rather than throwing.
    assert isinstance(snap.errors, dict)
    if snap.gpu is None:
        assert "gpu" in snap.errors


def test_no_router_configured_leaves_llama_none():
    builder = SnapshotBuilder(Settings(node_id="n1", llama_router_url=None))
    snap = builder.build()
    assert snap.runtimes.llama_cpp is None
    assert snap.runtimes.vllm == []


def test_health_is_assessed_from_collected_signals():
    builder = SnapshotBuilder(Settings(node_id="n1"))
    snap = builder.build()
    assert snap.health in set(HealthState)


def test_vllm_endpoints_parsed_from_comma_separated_env():
    settings = Settings(vllm_urls="http://a:8000/metrics, http://b:8001/metrics ")
    assert settings.vllm_endpoints == ["http://a:8000/metrics", "http://b:8001/metrics"]


def test_empty_vllm_urls_yields_no_endpoints():
    assert Settings(vllm_urls="").vllm_endpoints == []
    assert Settings(vllm_urls="  ,  ").vllm_endpoints == []
