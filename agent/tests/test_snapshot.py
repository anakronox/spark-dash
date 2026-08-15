"""Snapshot assembly: partial failure must degrade, not collapse.

Also covers host-procfs redirection — a container reading its own /proc reports
the container's memory view rather than the machine's, which is a bug that would
otherwise look completely plausible in production.
"""

from pathlib import Path

import psutil
import pytest
from spark_dash_agent.config import Settings
from spark_dash_agent.snapshot import (
    SnapshotBuilder,
    _point_psutil_at_host_proc,
    resolve_process_routers,
)
from spark_dash_common.models import (
    HealthState,
    LlamaRouterMetrics,
    ModelState,
    ProcessInfo,
    RouterModel,
)


def _router(endpoint, name="", **models):
    return LlamaRouterMetrics(
        endpoint=endpoint,
        name=name,
        models=[RouterModel(name=n, state=s) for n, s in models.items()],
    )


def _proc(model=None, pid=1):
    return ProcessInfo(
        pid=pid, name="llama-server", gpu_mem_bytes=1, runtime="llama.cpp", model=model
    )


class TestResolveProcessRouters:
    """Joins a process's --alias to the router that reports that model, so
    process memory can be correlated with the per-model router series."""

    def test_single_router_claiming_the_model(self):
        procs = resolve_process_routers(
            [_proc("qwen36-35b")],
            [_router("http://a:8000", "a:8001", **{"qwen36-35b": ModelState.ACTIVE})],
        )
        assert procs[0].router == "a:8001"

    def test_label_matches_the_exporter_falling_back_to_endpoint(self):
        """The exporter labels routers `name or endpoint`; these must agree or
        the two metric families won't join."""
        procs = resolve_process_routers(
            [_proc("qwen36-35b")],
            [_router("http://a:8000", "", **{"qwen36-35b": ModelState.ACTIVE})],
        )
        assert procs[0].router == "http://a:8000"

    def test_router_parent_without_a_model_is_left_alone(self):
        procs = resolve_process_routers(
            [_proc(None)],
            [_router("http://a:8000", "a", **{"qwen36-35b": ModelState.ACTIVE})],
        )
        assert procs[0].router is None

    def test_model_no_router_knows(self):
        procs = resolve_process_routers(
            [_proc("orphan")],
            [_router("http://a:8000", "a", **{"qwen36-35b": ModelState.ACTIVE})],
        )
        assert procs[0].router is None

    def test_ambiguous_model_resolved_by_which_router_has_it_active(self):
        """The same alias can be registered with several routers; the one
        actually holding weights is the one serving it."""
        procs = resolve_process_routers(
            [_proc("shared")],
            [
                _router("http://a:8000", "a", **{"shared": ModelState.UNLOADED}),
                _router("http://b:8000", "b", **{"shared": ModelState.ACTIVE}),
            ],
        )
        assert procs[0].router == "b"

    def test_ambiguous_and_unresolvable_is_left_unset(self):
        """Two routers both serving it — guessing would attribute the memory to
        the wrong one, which is worse than declining to say."""
        procs = resolve_process_routers(
            [_proc("shared")],
            [
                _router("http://a:8000", "a", **{"shared": ModelState.ACTIVE}),
                _router("http://b:8000", "b", **{"shared": ModelState.ACTIVE}),
            ],
        )
        assert procs[0].router is None

    def test_no_routers_at_all(self):
        procs = resolve_process_routers([_proc("qwen36-35b")], [])
        assert procs[0].router is None


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

    def _hostname_file(self, tmp_path, value: str):
        path = tmp_path / "hostname"
        path.write_text(value)
        return path

    def test_reads_host_hostname_from_etc_hostname(self, tmp_path):
        """Must be /etc/hostname, not /proc/sys/kernel/hostname.

        Reading it via procfs looked correct in a unit test with a fake
        directory but returned the CONTAINER's hostname on real Docker — the
        procfs entry is UTS-namespace-aware, so a bind-mounted host /proc
        doesn't help. On the GX10 that produced node_id "41fd7b9be4e6", the
        container id.
        """
        path = self._hostname_file(tmp_path, "sparky\n")
        assert Settings(node_id="", hostname_path=path).resolve_node_id() == "sparky"

    def test_procfs_hostname_is_not_used(self, tmp_path):
        """Guards the regression directly: a populated procfs must not be
        consulted, because in a container it lies."""
        kernel = tmp_path / "sys" / "kernel"
        kernel.mkdir(parents=True)
        (kernel / "hostname").write_text("container-id-abc\n")

        resolved = Settings(
            node_id="", proc_path=tmp_path, hostname_path=tmp_path / "absent"
        ).resolve_node_id()
        assert resolved != "container-id-abc"

    def test_hostname_whitespace_is_stripped(self, tmp_path):
        path = self._hostname_file(tmp_path, "  gx10-3  \n\n")
        assert Settings(node_id="", hostname_path=path).resolve_node_id() == "gx10-3"

    def test_only_first_line_is_used(self, tmp_path):
        path = self._hostname_file(tmp_path, "sparky\nstray junk\n")
        assert Settings(node_id="", hostname_path=path).resolve_node_id() == "sparky"

    def test_explicit_id_beats_hostname(self, tmp_path):
        """An override must still work for a node whose hostname is a poor
        label."""
        path = self._hostname_file(tmp_path, "ubuntu\n")
        assert Settings(node_id="gx10-1", hostname_path=path).resolve_node_id() == "gx10-1"

    def test_literal_unknown_is_treated_as_unset(self, tmp_path):
        """'unknown' was the old default; it must not stick as a real id."""
        path = self._hostname_file(tmp_path, "gx10-1\n")
        assert Settings(node_id="unknown", hostname_path=path).resolve_node_id() == "gx10-1"

    def test_blank_node_id_is_treated_as_unset(self, tmp_path):
        path = self._hostname_file(tmp_path, "gx10-1\n")
        assert Settings(node_id="   ", hostname_path=path).resolve_node_id() == "gx10-1"

    def test_falls_back_to_container_hostname(self, tmp_path):
        """Unreadable host hostname shouldn't leave the node unlabeled, but
        this is the bad path — the container hostname changes on recreate, so
        the agent logs an error rather than a shrug."""
        resolved = Settings(node_id="", hostname_path=tmp_path / "missing").resolve_node_id()
        assert resolved
        assert resolved != "unknown"

    def test_builder_resolves_once(self, tmp_path):
        path = self._hostname_file(tmp_path, "gx10-1\n")

        builder = SnapshotBuilder(Settings(node_id="", hostname_path=path))
        assert builder.node_id == "gx10-1"
        assert builder.build().node_id == "gx10-1"

        # Identity can't change while the process runs, so a later hostname
        # edit must not retroactively relabel the node's metrics.
        path.write_text("something-else\n")
        assert builder.build().node_id == "gx10-1"


def test_agent_version_is_reported(tmp_path):
    """Baked into the image at build time. A stale agent otherwise presents as
    a missing feature rather than as a stale agent — which has cost real
    debugging time."""
    builder = SnapshotBuilder(Settings(node_id="n1", agent_version="abc1234"))
    assert builder.build().agent_version == "abc1234"


def test_agent_version_defaults_to_unknown():
    """Running from source, there's no commit to name — and saying 'unknown' is
    honest where inventing a version would not be."""
    assert SnapshotBuilder(Settings(node_id="n1")).build().agent_version == "unknown"
