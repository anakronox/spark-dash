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
    apply_interface_policy,
    detect_unmonitored_runtimes,
    resolve_process_servers,
)
from spark_dash_common.models import (
    EngineMetrics,
    HealthState,
    LlamaRouterMetrics,
    ModelState,
    NetworkInterface,
    ProcessInfo,
    RdmaPort,
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


class TestResolveProcessServers:
    """Joins a process's --alias to the router that reports that model, so
    process memory can be correlated with the per-model router series."""

    def test_single_router_claiming_the_model(self):
        procs = resolve_process_servers(
            [_proc("qwen36-35b")],
            [_router("http://a:8000", "a:8001", **{"qwen36-35b": ModelState.ACTIVE})],
        )
        assert procs[0].server == "a:8001"

    def test_label_matches_the_exporter_falling_back_to_endpoint(self):
        """The exporter labels routers `name or endpoint`; these must agree or
        the two metric families won't join."""
        procs = resolve_process_servers(
            [_proc("qwen36-35b")],
            [_router("http://a:8000", "", **{"qwen36-35b": ModelState.ACTIVE})],
        )
        assert procs[0].server == "http://a:8000"

    def test_router_parent_without_a_model_is_left_alone(self):
        procs = resolve_process_servers(
            [_proc(None)],
            [_router("http://a:8000", "a", **{"qwen36-35b": ModelState.ACTIVE})],
        )
        assert procs[0].server is None

    def test_model_no_router_knows(self):
        procs = resolve_process_servers(
            [_proc("orphan")],
            [_router("http://a:8000", "a", **{"qwen36-35b": ModelState.ACTIVE})],
        )
        assert procs[0].server is None

    def test_ambiguous_model_resolved_by_which_router_has_it_active(self):
        """The same alias can be registered with several routers; the one
        actually holding weights is the one serving it."""
        procs = resolve_process_servers(
            [_proc("shared")],
            [
                _router("http://a:8000", "a", **{"shared": ModelState.UNLOADED}),
                _router("http://b:8000", "b", **{"shared": ModelState.ACTIVE}),
            ],
        )
        assert procs[0].server == "b"

    def test_ambiguous_and_unresolvable_is_left_unset(self):
        """Two routers both serving it — guessing would attribute the memory to
        the wrong one, which is worse than declining to say."""
        procs = resolve_process_servers(
            [_proc("shared")],
            [
                _router("http://a:8000", "a", **{"shared": ModelState.ACTIVE}),
                _router("http://b:8000", "b", **{"shared": ModelState.ACTIVE}),
            ],
        )
        assert procs[0].server is None

    def test_no_routers_at_all(self):
        procs = resolve_process_servers([_proc("qwen36-35b")], [])
        assert procs[0].server is None


class TestVllmAttribution:
    """vLLM can't be resolved the way llama.cpp is.

    It rewrites its process title to a bare `VLLM::EngineCore` with NO
    arguments — verified on the GX10 — so there is nothing in argv to parse.
    The model name only exists in the instance's own /metrics, so the join is
    by count instead of by identity.
    """

    def _engine(self, pid=99):
        return ProcessInfo(
            pid=pid, name="VLLM::EngineCore", gpu_mem_bytes=1, runtime="vllm"
        )

    def test_single_instance_names_the_model_and_server(self):
        procs = resolve_process_servers(
            [self._engine()],
            [],
            {"vllm": [EngineMetrics(model="qwen36-35b-heretic", server="192.168.50.61:8120")]},
        )
        assert procs[0].model == "qwen36-35b-heretic"
        assert procs[0].server == "192.168.50.61:8120"

    def test_several_instances_are_left_unattributed(self):
        """Two engines and two instances can't be matched without
        cross-namespace socket inspection. Declining beats guessing, since a
        wrong answer here misattributes GPU memory to the wrong model."""
        procs = resolve_process_servers(
            [self._engine(1), self._engine(2)],
            [],
            {
                "vllm": [
                    EngineMetrics(model="a", server="h:8120"),
                    EngineMetrics(model="b", server="h:8121"),
                ]
            },
        )
        assert all(p.model is None and p.server is None for p in procs)

    def test_llama_processes_are_untouched_by_the_vllm_pass(self):
        """A node runs both. The vLLM fallback must not overwrite an
        attribution the router join already made correctly."""
        procs = resolve_process_servers(
            [_proc("qwen36-35b")],
            [_router("http://a:8000", "a:8001", **{"qwen36-35b": ModelState.ACTIVE})],
            {"vllm": [EngineMetrics(model="something-else", server="h:8120")]},
        )
        assert procs[0].model == "qwen36-35b"
        assert procs[0].server == "a:8001"


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


def test_engine_endpoints_parsed_from_comma_separated_env():
    settings = Settings(
        vllm_urls="http://a:8000/metrics, http://b:8001/metrics ",
        sglang_urls="http://c:30000/metrics",
    )
    assert settings.engine_endpoints("vllm") == [
        "http://a:8000/metrics",
        "http://b:8001/metrics",
    ]
    assert settings.engine_endpoints("sglang") == ["http://c:30000/metrics"]


def test_an_unknown_engine_has_no_endpoints_rather_than_raising():
    """The agent can be older than the backend naming an engine to it. Nothing
    configured is the honest answer; a crash on a node running a newer stack is
    not."""
    assert Settings().engine_endpoints("some-future-engine") == []


def test_empty_vllm_urls_yields_no_endpoints():
    assert Settings(vllm_urls="").engine_endpoints("vllm") == []
    assert Settings(vllm_urls="  ,  ").engine_endpoints("vllm") == []


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


class TestUnmonitoredRuntimes:
    """The gap between what is running on the GPU and what is configured to be
    collected from. Catches a silence — an unmonitored server looks like an
    absence rather than an error, so nothing else reports it.
    """

    def _proc(self, runtime, pid=1):
        return ProcessInfo(pid=pid, name="x", gpu_mem_bytes=1, runtime=runtime)

    def test_vllm_running_with_nothing_configured(self):
        """The real case: a vLLM container ran on sparky for an unknown period
        holding GPU memory, with no throughput or queue data reaching the
        dashboard, and nothing said so."""
        gaps = detect_unmonitored_runtimes(
            [self._proc("vllm")], configured={"llama.cpp"}
        )
        assert gaps == ["vllm"]

    def test_nothing_flagged_when_configured(self):
        gaps = detect_unmonitored_runtimes(
            [self._proc("vllm")], configured={"vllm"}
        )
        assert gaps == []

    def test_compares_against_configuration_not_collection_success(self):
        """A configured endpoint that is momentarily erroring must not raise a
        gap warning — that would turn a transient scrape failure into a
        misconfiguration report."""
        gaps = detect_unmonitored_runtimes(
            [self._proc("vllm")], configured={"vllm"}
        )
        assert gaps == []

    def test_runtimes_with_no_collector_are_not_flagged(self):
        """Atlas, TGI and ollama have nothing to configure, so flagging them
        would produce a warning that can never be resolved — which teaches the
        reader to ignore the indicator entirely."""
        gaps = detect_unmonitored_runtimes(
            [self._proc("atlas"), self._proc("ollama", pid=2)],
            configured=set(),
        )
        assert gaps == []

    def test_sglang_is_flagged_now_that_it_has_a_collector(self):
        """It was excluded for exactly as long as there was nothing to
        configure. There is now, so an SGLang server running with no endpoint
        configured is a resolvable gap rather than noise."""
        gaps = detect_unmonitored_runtimes([self._proc("sglang")], configured=set())
        assert gaps == ["sglang"]

    def test_non_llm_workloads_are_irrelevant(self):
        """ComfyUI holds GPU memory but is not an inference server; there is no
        endpoint to configure for it."""
        gaps = detect_unmonitored_runtimes(
            [self._proc("comfyui")], configured=set()
        )
        assert gaps == []

    def test_both_runtimes_unconfigured(self):
        gaps = detect_unmonitored_runtimes(
            [self._proc("vllm"), self._proc("llama.cpp", pid=2)],
            configured=set(),
        )
        assert gaps == ["llama.cpp", "vllm"]

    def test_several_engine_processes_report_one_gap(self):
        """One vLLM instance spawns several processes. The gap is per RUNTIME,
        not per process, so this must not report the same thing twice."""
        gaps = detect_unmonitored_runtimes(
            [self._proc("vllm", 1), self._proc("vllm", 2), self._proc("vllm", 3)],
            configured=set(),
        )
        assert gaps == ["vllm"]

    def test_idle_node_reports_nothing(self):
        assert detect_unmonitored_runtimes([], configured=set()) == []


class TestGapDetectionUnderCentralConfig:
    """The regression the migration exposed.

    Once a node is managed centrally its env is empty by design. Checking the
    environment alone therefore reported every running runtime as unmonitored
    the moment the migration completed — a false positive on exactly the
    configuration the feature exists to support.
    """

    def test_central_config_counts_as_configured(self):
        from spark_dash_agent.remote_config import NodeConfig

        builder = SnapshotBuilder(Settings(node_id="n1", llama_router_urls=""))
        builder._applied = NodeConfig(llama_routers=["http://r:8001"])

        procs = [ProcessInfo(pid=1, name="llama-server", gpu_mem_bytes=1, runtime="llama.cpp")]
        gaps = detect_unmonitored_runtimes(
            procs, configured=builder._configured_runtimes()
        )
        assert gaps == [], "central-configured routers must not read as unmonitored"

    def test_central_config_covers_every_engine(self):
        """The same rule, for an engine the deployment gained later: an SGLang
        endpoint that arrived from cluster.yml must count as configured even
        though the node's own env names nothing."""
        from spark_dash_agent.remote_config import NodeConfig

        builder = SnapshotBuilder(Settings(node_id="n1"))
        builder._applied = NodeConfig(
            engines={"sglang": ["http://s:30000/metrics"]}
        )
        assert builder._configured_runtimes() == {"sglang"}

    def test_env_still_counts_when_central_is_absent(self):
        """A node not yet migrated keeps working off its environment."""
        procs = [ProcessInfo(pid=1, name="llama-server", gpu_mem_bytes=1, runtime="llama.cpp")]
        assert detect_unmonitored_runtimes(
            procs, configured={"llama.cpp"}
        ) == []


class TestInterfacePolicy:
    """Which interfaces alerting watches.

    The failure this exists for: two 200Gb ports per node were cabled to a
    switch as a test and then deliberately unplugged. Having been up, they read
    as links that FAILED to the 7-day heuristic and alerted indefinitely — four
    NetworkLinkDown plus four RdmaPortDown across two nodes, measured
    2026-08-21. Nothing in the history distinguishes "I unplugged this" from
    "this died", so intent has to be configured.
    """

    def _iface(self, name):
        return NetworkInterface(name=name, up=False)

    def _port(self, device, interface):
        return RdmaPort(device=device, port=1, interface=interface)

    def test_named_interfaces_are_excluded(self):
        network = [self._iface("enP2p1s0f0np0"), self._iface("enP2p1s0f1np1")]
        apply_interface_policy(network, [], {"enP2p1s0f1np1"})
        assert [i.monitored for i in network] == [True, False]

    def test_everything_is_watched_by_default(self):
        """Excluded by name, never selected by name: an interface nobody has
        configured still alerts, so forgetting the list is noisy rather than
        silent."""
        network = [self._iface("enP7s7"), self._iface("wlP9s9")]
        apply_interface_policy(network, [], set())
        assert all(i.monitored for i in network)

    def test_excluded_interfaces_are_still_reported(self):
        """Marked, not filtered. An interface that vanished from the panel
        would be worse than one that is quiet in it — and its link history has
        to keep accumulating for the day someone plugs it back in."""
        network = [self._iface("enP2p1s0f1np1")]
        apply_interface_policy(network, [], {"enP2p1s0f1np1"})
        assert len(network) == 1
        assert network[0].name == "enP2p1s0f1np1"

    def test_a_roce_port_inherits_its_netdevs_policy(self):
        """One cable carries both. Excluding the interface without excluding
        its port would trade NetworkLinkDown for RdmaPortDown and change
        nothing an operator would notice — which is half of what was firing."""
        network = [self._iface("enP2p1s0f1np1")]
        rdma = [
            self._port("roceP2p1s0f0", "enP2p1s0f0np0"),
            self._port("roceP2p1s0f1", "enP2p1s0f1np1"),
        ]
        apply_interface_policy(network, rdma, {"enP2p1s0f1np1"})
        assert [p.monitored for p in rdma] == [True, False]

    def test_an_unpaired_roce_port_stays_monitored(self):
        """`interface` is empty when the RoCE device has no netdev under its
        PCI function. Defaulting to quiet there would hide a fabric port nobody
        chose to hide."""
        rdma = [self._port("roceX", "")]
        apply_interface_policy([], rdma, {"enP2p1s0f1np1"})
        assert rdma[0].monitored is True

    def test_a_name_matching_nothing_is_not_an_error(self):
        """Ordinary during a rename, or while a NIC is absent. The config keeps
        the entry so the exclusion survives the interface coming back."""
        network = [self._iface("enP7s7")]
        apply_interface_policy(network, [], {"enP9s9-gone"})
        assert network[0].monitored is True


class TestClusterScopedCollection:
    """A distributed model is served by the CLUSTER, not by a node.

    `danflashes` runs one vLLM model across sparketa and sparkjr. sparketa
    holds the API; sparkjr is a tensor-parallel worker holding 96.8 GiB with no
    endpoint of its own — and there is no endpoint to configure for it, ever.
    Scoped to the node, the gap check flagged it every poll and no edit could
    clear the warning, which is exactly what `COLLECTIBLE_RUNTIMES` excludes
    Atlas and ollama to avoid.
    """

    def _builder(self, **cfg):
        from spark_dash_agent.remote_config import NodeConfig

        builder = SnapshotBuilder(Settings(node_id="sparkjr"))
        builder._applied = NodeConfig(**cfg)
        return builder

    def test_a_peer_collecting_it_counts_as_configured(self):
        builder = self._builder(cluster_collected_runtimes=["vllm"])
        assert builder._configured_runtimes() == {"vllm"}
        procs = [ProcessInfo(pid=1, name="VLLM::EngineCore", gpu_mem_bytes=1, runtime="vllm")]
        assert detect_unmonitored_runtimes(
            procs, configured=builder._configured_runtimes()
        ) == []

    def test_retiring_the_head_node_re_arms_the_warning(self):
        """The property that keeps this honest. If the only configured endpoint
        in the cluster is removed, no peer is collecting it either, so every
        node starts flagging again rather than staying quiet forever."""
        builder = self._builder(cluster_collected_runtimes=[])
        procs = [ProcessInfo(pid=1, name="VLLM::EngineCore", gpu_mem_bytes=1, runtime="vllm")]
        assert detect_unmonitored_runtimes(
            procs, configured=builder._configured_runtimes()
        ) == ["vllm"]

    def test_a_standalone_node_is_unaffected(self):
        """`sparky` is in no cluster, so it has no peers and nothing is
        suppressed — an unmonitored runtime there is still a real gap."""
        builder = self._builder(llama_routers=["http://r:8001"])
        procs = [ProcessInfo(pid=1, name="python", gpu_mem_bytes=1, runtime="vllm")]
        assert detect_unmonitored_runtimes(
            procs, configured=builder._configured_runtimes()
        ) == ["vllm"]

    def test_a_peer_collecting_one_engine_does_not_excuse_another(self):
        """Per runtime, not a blanket cluster exemption. A cluster collecting
        vLLM says nothing about an unmonitored SGLang server on one of its
        nodes."""
        builder = self._builder(cluster_collected_runtimes=["vllm"])
        procs = [
            ProcessInfo(pid=1, name="VLLM::EngineCore", gpu_mem_bytes=1, runtime="vllm"),
            ProcessInfo(pid=2, name="python", gpu_mem_bytes=1, runtime="sglang"),
        ]
        assert detect_unmonitored_runtimes(
            procs, configured=builder._configured_runtimes()
        ) == ["sglang"]


class TestIgnoredInterfacesComeFromCentralConfig:
    def test_central_config_supplies_the_list(self):
        from spark_dash_agent.remote_config import NodeConfig

        builder = SnapshotBuilder(Settings(node_id="n1"))
        builder._applied = NodeConfig(ignored_interfaces=["enP2p1s0f1np1"])
        assert builder._ignored_interfaces() == {"enP2p1s0f1np1"}

    def test_an_unconfigured_node_watches_everything(self):
        """Central-only, deliberately: a node not in cluster.yml keeps the
        historical behaviour, and the node stack stays identical everywhere
        rather than gaining a per-node variable for a decision the dashboard
        owns."""
        assert SnapshotBuilder(Settings(node_id="n1"))._ignored_interfaces() == set()
