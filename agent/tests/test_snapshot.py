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


def test_no_router_configured_yields_empty_runtime_lists():
    """vLLM-only nodes run the same image with no router URLs set."""
    builder = SnapshotBuilder(Settings(node_id="n1", llama_router_urls=""))
    snap = builder.build()
    assert snap.runtimes.llama_cpp == []
    assert snap.runtimes.vllm == []


def test_router_endpoints_parsed_from_comma_separated_env():
    settings = Settings(llama_router_urls="http://a:8080, http://b:8081 ")
    assert settings.llama_router_endpoints == ["http://a:8080", "http://b:8081"]


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


class TestNodeIdResolution:
    """One stack repo serves all three GX10s because the node identifies
    itself. A per-node override would mean either three repos or a variable
    that's easy to forget — and forgetting it merges three nodes' metrics into
    one series.
    """

    def test_explicit_node_id_wins(self):
        assert Settings(node_id="gx10-1").resolve_node_id() == "gx10-1"

    def test_reads_host_hostname_from_mounted_proc(self, tmp_path):
        """The host's hostname, not the container's — which is why this reads
        the mounted procfs rather than calling gethostname()."""
        kernel = tmp_path / "sys" / "kernel"
        kernel.mkdir(parents=True)
        (kernel / "hostname").write_text("gx10-2\n")

        assert Settings(node_id="", proc_path=tmp_path).resolve_node_id() == "gx10-2"

    def test_hostname_whitespace_is_stripped(self, tmp_path):
        kernel = tmp_path / "sys" / "kernel"
        kernel.mkdir(parents=True)
        (kernel / "hostname").write_text("  gx10-3  \n\n")

        assert Settings(node_id="", proc_path=tmp_path).resolve_node_id() == "gx10-3"

    def test_explicit_id_beats_hostname(self, tmp_path):
        """An override must still work for a node whose hostname is a poor
        label."""
        kernel = tmp_path / "sys" / "kernel"
        kernel.mkdir(parents=True)
        (kernel / "hostname").write_text("ubuntu\n")

        settings = Settings(node_id="gx10-1", proc_path=tmp_path)
        assert settings.resolve_node_id() == "gx10-1"

    def test_literal_unknown_is_treated_as_unset(self, tmp_path):
        """'unknown' was the old default; it must not stick as a real id."""
        kernel = tmp_path / "sys" / "kernel"
        kernel.mkdir(parents=True)
        (kernel / "hostname").write_text("gx10-1\n")

        assert Settings(node_id="unknown", proc_path=tmp_path).resolve_node_id() == "gx10-1"

    def test_blank_node_id_is_treated_as_unset(self, tmp_path):
        kernel = tmp_path / "sys" / "kernel"
        kernel.mkdir(parents=True)
        (kernel / "hostname").write_text("gx10-1\n")

        assert Settings(node_id="   ", proc_path=tmp_path).resolve_node_id() == "gx10-1"

    def test_falls_back_to_container_hostname(self, tmp_path):
        """Unreadable host procfs shouldn't leave the node unlabeled, but the
        agent logs a warning: a container hostname changes on recreate."""
        resolved = Settings(node_id="", proc_path=tmp_path / "missing").resolve_node_id()
        assert resolved
        assert resolved != "unknown"

    def test_builder_resolves_once(self, tmp_path):
        kernel = tmp_path / "sys" / "kernel"
        kernel.mkdir(parents=True)
        (kernel / "hostname").write_text("gx10-1\n")

        builder = SnapshotBuilder(Settings(node_id="", proc_path=tmp_path))
        assert builder.node_id == "gx10-1"
        assert builder.build().node_id == "gx10-1"

        # Identity can't change while the process runs, so a later hostname
        # edit must not retroactively relabel the node's metrics.
        (kernel / "hostname").write_text("something-else\n")
        assert builder.build().node_id == "gx10-1"
