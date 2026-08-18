"""Assembles one `NodeSnapshot` from all collectors.

Every collector runs through `safe_collect`, so one failing source degrades that
section to `None` (with the reason recorded in `errors`) rather than taking down
the snapshot. A node with a broken router should still show GPU and memory.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psutil
from spark_dash_common.health import assess
from spark_dash_common.models import (
    ConfigStatus,
    LlamaRouterMetrics,
    ModelState,
    NodeSnapshot,
    ProcessInfo,
    Runtimes,
    TempBands,
    VllmMetrics,
)
from spark_dash_common.thresholds import TempThresholds

from spark_dash_agent.collectors.cpu import CpuCollector, read_critical_trip_c
from spark_dash_agent.collectors.disk import DiskCollector
from spark_dash_agent.collectors.gpu import GpuCollector
from spark_dash_agent.collectors.llama_router import LlamaRouterCollector
from spark_dash_agent.collectors.memory import MemoryCollector, detect_unified_memory
from spark_dash_agent.collectors.network import NetworkCollector, RdmaCollector
from spark_dash_agent.collectors.psi import PsiCollector
from spark_dash_agent.collectors.vllm import VllmCollector
from spark_dash_agent.config import Settings
from spark_dash_agent.remote_config import RemoteConfig, RuntimeConfig

log = logging.getLogger(__name__)


def resolve_process_servers(
    processes: list[ProcessInfo],
    routers: list[LlamaRouterMetrics],
    vllm: list[VllmMetrics] | None = None,
) -> list[ProcessInfo]:
    """Attach the serving host:port — and for vLLM, the model — to each process.

    The join is by model name, because a llama.cpp child's `--alias` is the
    same string its router reports. The label used matches the exporter's
    `router` label exactly (`name or endpoint`), which is what lets process
    memory be correlated with the per-model router series in one query.

    Ambiguity is left unresolved rather than guessed at. A node runs several
    routers and the same model name can be registered with more than one, so:

      - exactly one router knows the model  -> attribute it
      - several know it, one has it ACTIVE  -> that's the one holding weights
      - several, none or many ACTIVE        -> leave it unset

    The parent's PID is not used to break ties. A child's parent is a router
    process listening on a container-internal port, and mapping that back to
    the host-side endpoint would need cross-namespace socket inspection for a
    case that resolves on ACTIVE state anyway.
    """
    # NOT an early return when there are no routers: a vLLM-only node has
    # none, and skipping the pass below would leave every engine process
    # unattributed on exactly the deployment vLLM support is for.
    by_model: dict[str, list[LlamaRouterMetrics]] = {}
    for router in routers:
        for model in router.models:
            by_model.setdefault(model.name, []).append(router)

    def _label(router: LlamaRouterMetrics) -> str:
        return router.name or router.endpoint

    def _has_active(router: LlamaRouterMetrics, model_name: str) -> bool:
        return any(m.name == model_name and m.state is ModelState.ACTIVE for m in router.models)

    for proc in processes:
        if not proc.model:
            continue
        candidates = by_model.get(proc.model, [])
        if len(candidates) == 1:
            proc.server = _label(candidates[0])
            continue
        active = [r for r in candidates if _has_active(r, proc.model)]
        if len(active) == 1:
            proc.server = _label(active[0])
        elif candidates:
            log.debug(
                "model %r is ambiguous across %d routers; leaving server unset",
                proc.model,
                len(candidates),
            )

    _resolve_vllm(processes, vllm or [])
    return processes


def _resolve_vllm(processes: list[ProcessInfo], vllm: list[VllmMetrics]) -> None:
    """Name the model and server for vLLM processes.

    vLLM cannot be resolved the way llama.cpp is. It rewrites its process title
    to a bare `VLLM::EngineCore` with NO arguments at all, so there is nothing
    in argv to parse — verified on the GX10. The model name is only available
    from the instance's own /metrics, which the vllm collector already scraped.

    So the join is by count rather than by identity: with exactly one instance
    configured, every vLLM process on the node belongs to it. With several,
    there is no way to tell which engine serves which without cross-namespace
    socket inspection, so they are left unattributed rather than guessed at —
    the same rule the router join follows.
    """
    if len(vllm) != 1:
        if len(vllm) > 1:
            log.debug(
                "%d vLLM instances; cannot attribute engine processes to one", len(vllm)
            )
        return

    instance = vllm[0]
    for proc in processes:
        if proc.runtime == "vllm":
            proc.model = proc.model or instance.model
            proc.server = proc.server or instance.server


#: Runtimes this agent has a collector for. A gap is only actionable if there
#: is something to configure — see `detect_unmonitored_runtimes`.
COLLECTIBLE_RUNTIMES = frozenset({"llama.cpp", "vllm"})


def detect_unmonitored_runtimes(
    processes: list[ProcessInfo],
    *,
    llama_configured: bool,
    vllm_configured: bool,
) -> list[str]:
    """Runtimes observed on the GPU that nothing is configured to collect from.

    Both halves already exist in every snapshot: NVML sees each process and
    infers its runtime, and the settings say which endpoints are configured.
    The delta is the gap — and it is the failure nothing else reports, because
    an unmonitored server looks like an absence rather than an error.

    Compares against what is CONFIGURED, not what was successfully collected.
    A vLLM endpoint that is configured but momentarily erroring drops out of
    the collected list, and treating that as "unconfigured" would raise a gap
    warning for a transient scrape failure.

    Deliberately does NOT try to match listening ports against configured
    ports. A process's port is not readable across the network namespace — the
    same wall hit when attributing vLLM processes to models — so the rule is
    coarser: flag a runtime only when *nothing at all* is configured for it.
    That catches the completely-unmonitored case, which is the one that
    matters, and cannot false-positive when one instance spawns several
    engine processes.
    """
    configured = set()
    if llama_configured:
        configured.add("llama.cpp")
    if vllm_configured:
        configured.add("vllm")

    observed = {
        p.runtime for p in processes if p.runtime in COLLECTIBLE_RUNTIMES
    }
    return sorted(observed - configured)


def _point_psutil_at_host_proc(proc_path: Path) -> None:
    """Redirect psutil to the host's procfs.

    Without this the agent reports the *container's* memory view, which on a
    container with no memory limit still differs subtly and is conceptually the
    wrong thing to monitor. Linux-only; psutil has no such knob elsewhere.
    """
    if not hasattr(psutil, "PROCFS_PATH"):
        return
    if proc_path != Path("/proc") and proc_path.exists():
        psutil.PROCFS_PATH = str(proc_path)
        log.info("reading host procfs from %s", proc_path)


class SnapshotBuilder:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        _point_psutil_at_host_proc(settings.proc_path)

        # Resolved once: the node's identity can't change while it runs, and
        # re-reading it per tick would be noise in the logs.
        self._node_id = settings.resolve_node_id()

        self._gpu = GpuCollector(device_index=settings.gpu_device_index)
        self._psi = PsiCollector(path=settings.proc_path / "pressure" / "memory")
        self._cpu = CpuCollector()
        self._llama = LlamaRouterCollector(
            settings.llama_router_endpoints,
            timeout=settings.llama_router_timeout_s,
            budget_s=settings.runtime_collect_budget_s,
            metrics_allowlist=settings.llama_metrics_allowlist,
        )
        self._vllm = VllmCollector(
            settings.vllm_endpoints, budget_s=settings.runtime_collect_budget_s
        )

        # Central config, if a backend is configured. Built after the
        # collectors so the env-derived ones above are the starting point and
        # the fallback.
        self._remote = RemoteConfig(
            settings.backend_url,
            self._node_id,
            ttl_s=settings.cluster_config_ttl_s,
        )
        self._applied: RuntimeConfig | None = None
        self._disk = DiskCollector(settings.root_path)
        self._network = NetworkCollector(settings.sys_path)
        self._rdma = RdmaCollector(settings.sys_path)

        # The CPU's critical trip doesn't change while the machine runs, so
        # read it once. The GPU's limits need an open NVML handle and so are
        # resolved lazily in `_temp_bands`.
        self._cpu_trip_c = read_critical_trip_c(settings.sys_path)
        log.info("CPU critical trip point: %s", self._cpu_trip_c)
        # Built lazily: UMA detection needs NVML's total, which isn't known
        # until the GPU collector has opened the device once.
        self._memory: MemoryCollector | None = None

    def _apply_remote_config(self, now: float) -> None:
        """Rebuild the runtime collectors when central config changes.

        Rebuilt rather than mutated because both collectors hold rate-tracking
        state keyed by endpoint, and an endpoint that has changed has no
        meaningful history to carry over. Only done when the config actually
        differs, so a steady state never resets rates.
        """
        runtimes = self._remote.current(now)
        if runtimes is None or runtimes == self._applied:
            return

        log.info(
            "applying cluster config: routers=%s vllm=%s",
            runtimes.llama_routers or "none",
            runtimes.vllm or "none",
        )
        self._llama = LlamaRouterCollector(
            runtimes.llama_routers,
            timeout=self._settings.llama_router_timeout_s,
            budget_s=self._settings.runtime_collect_budget_s,
            metrics_allowlist=runtimes.metrics_allowlist,
        )
        self._vllm = VllmCollector(
            runtimes.vllm, budget_s=self._settings.runtime_collect_budget_s
        )
        self._applied = runtimes

    def _config_status(self) -> ConfigStatus:
        """Where this node's runtimes came from, as wall-clock time.

        RemoteConfig works in monotonic time — correct for scheduling, since it
        cannot jump when the clock is stepped, and useless for display. The
        offset is computed here rather than stored so a clock correction shows
        up immediately instead of being baked in at fetch time.
        """
        source, last_ok = self._remote.status(time.monotonic())
        fetched_at = None
        if last_ok is not None:
            age_s = max(0.0, time.monotonic() - last_ok)
            fetched_at = datetime.now(UTC) - timedelta(seconds=age_s)
        return ConfigStatus(source=source, fetched_at=fetched_at)

    def _temp_bands(self) -> tuple[TempThresholds, TempThresholds]:
        """GPU and CPU temperature bands, in precedence order.

        Explicit override, else derived from the hardware, else the fallback
        constants. Separate per component because a GB10 GPU throttles at 86C
        while the CPU beside it is rated to 104C — one shared pair could not be
        right for both, and wasn't.
        """
        gpu = self._settings.temp_thresholds or TempThresholds.for_gpu(
            self._gpu.slowdown_temp_c
        )
        cpu = self._settings.cpu_temp_thresholds or TempThresholds.for_cpu(self._cpu_trip_c)
        return gpu, cpu

    @property
    def node_id(self) -> str:
        return self._node_id

    def _memory_collector(self) -> MemoryCollector:
        if self._memory is None:
            unified = detect_unified_memory(
                self._gpu.memory_total_bytes, psutil.virtual_memory().total
            )
            if unified:
                log.info("coherent unified memory detected; using MemAvailable for GPU memory")
            self._memory = MemoryCollector(unified=unified)
        return self._memory

    def build(self) -> NodeSnapshot:
        errors: dict[str, str] = {}
        self._apply_remote_config(time.monotonic())

        # GPU first: it populates the NVML total that UMA detection depends on.
        gpu = self._gpu.safe_collect(errors)
        processes = []
        if gpu is not None:
            try:
                processes = self._gpu.collect_processes()
            except Exception as exc:  # noqa: BLE001 — process listing is best-effort
                errors["gpu_processes"] = f"{type(exc).__name__}: {exc}"

        memory = self._memory_collector().safe_collect(errors)
        disk = self._disk.safe_collect(errors)
        psi = self._psi.safe_collect(errors)
        cpu = self._cpu.safe_collect(errors)
        network = self._network.safe_collect(errors) or []
        rdma = self._rdma.safe_collect(errors) or []
        # Tell the router collector which models are actually working, so it
        # only scrapes those. `/metrics?model=` resets the router's idle timer,
        # so scraping an idle-but-loaded model would keep it resident forever —
        # measured, see `LlamaRouterCollector._busy_models`. NVML's per-process
        # SM view is independent of the router, which is what makes it a safe
        # signal to gate on.
        self._llama.set_busy_models(
            {p.model for p in processes if p.model and p.sm_pct > 0}
        )
        llama = self._llama.safe_collect(errors) or []
        vllm = self._vllm.safe_collect(errors) or []

        # Needs both collectors' output, so it happens here rather than inside
        # either one.
        processes = resolve_process_servers(processes, llama, vllm)

        # Against the EFFECTIVE config, not the environment.
        #
        # Once a node is managed centrally its env is empty by design, so
        # checking `settings` alone reported every running runtime as
        # unmonitored the moment the migration completed — a false positive on
        # exactly the configuration this feature is meant to support.
        applied = self._applied
        unmonitored = detect_unmonitored_runtimes(
            processes,
            llama_configured=bool(
                applied.llama_routers if applied else self._settings.llama_router_endpoints
            ),
            vllm_configured=bool(
                applied.vllm if applied else self._settings.vllm_endpoints
            ),
        )
        if unmonitored:
            log.warning(
                "inference runtime(s) running with nothing configured to "
                "collect from them: %s",
                ", ".join(unmonitored),
            )

        gpu_bands, cpu_bands = self._temp_bands()
        health, reasons = assess(
            gpu=gpu,
            memory=memory,
            psi=psi,
            cpu_temp_c=cpu.temp_c if cpu else None,
            temps=gpu_bands,
            cpu_temps=cpu_bands,
        )

        return NodeSnapshot(
            node_id=self._node_id,
            ts=datetime.now(UTC),
            up=True,
            agent_version=self._settings.agent_version,
            health=health,
            health_reasons=reasons,
            unmonitored_runtimes=unmonitored,
            config=self._config_status(),
            temp_bands=TempBands(
                gpu_warning_c=gpu_bands.warning_c,
                gpu_critical_c=gpu_bands.critical_c,
                gpu_source=gpu_bands.source,
                cpu_warning_c=cpu_bands.warning_c,
                cpu_critical_c=cpu_bands.critical_c,
                cpu_source=cpu_bands.source,
            ),
            gpu=gpu,
            memory=memory,
            disk=disk,
            psi=psi,
            cpu=cpu,
            processes=processes,
            network=network,
            rdma=rdma,
            runtimes=Runtimes(llama_cpp=llama, vllm=vllm),
            errors=errors,
        )
