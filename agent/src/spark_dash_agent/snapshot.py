"""Assembles one `NodeSnapshot` from all collectors.

Every collector runs through `safe_collect`, so one failing source degrades that
section to `None` (with the reason recorded in `errors`) rather than taking down
the snapshot. A node with a broken router should still show GPU and memory.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import psutil
from spark_dash_common.health import assess
from spark_dash_common.models import NodeSnapshot, Runtimes

from spark_dash_agent.collectors.cpu import CpuCollector
from spark_dash_agent.collectors.gpu import GpuCollector
from spark_dash_agent.collectors.llama_router import LlamaRouterCollector
from spark_dash_agent.collectors.memory import MemoryCollector, detect_unified_memory
from spark_dash_agent.collectors.psi import PsiCollector
from spark_dash_agent.collectors.vllm import VllmCollector
from spark_dash_agent.config import Settings

log = logging.getLogger(__name__)


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

        self._gpu = GpuCollector(device_index=settings.gpu_device_index)
        self._psi = PsiCollector(path=settings.proc_path / "pressure" / "memory")
        self._cpu = CpuCollector()
        self._llama = LlamaRouterCollector(
            settings.llama_router_endpoints,
            timeout=settings.llama_router_timeout_s,
            scrape_loaded_model_metrics=settings.llama_scrape_loaded_model_metrics,
        )
        self._vllm = VllmCollector(settings.vllm_endpoints)
        # Built lazily: UMA detection needs NVML's total, which isn't known
        # until the GPU collector has opened the device once.
        self._memory: MemoryCollector | None = None

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

        # GPU first: it populates the NVML total that UMA detection depends on.
        gpu = self._gpu.safe_collect(errors)
        processes = []
        if gpu is not None:
            try:
                processes = self._gpu.collect_processes()
            except Exception as exc:  # noqa: BLE001 — process listing is best-effort
                errors["gpu_processes"] = f"{type(exc).__name__}: {exc}"

        memory = self._memory_collector().safe_collect(errors)
        psi = self._psi.safe_collect(errors)
        cpu = self._cpu.safe_collect(errors)
        llama = self._llama.safe_collect(errors) or []
        vllm = self._vllm.safe_collect(errors) or []

        health, reasons = assess(
            gpu=gpu,
            memory=memory,
            psi=psi,
            cpu_temp_c=cpu.temp_c if cpu else None,
        )

        return NodeSnapshot(
            node_id=self._settings.node_id,
            ts=datetime.now(UTC),
            up=True,
            health=health,
            health_reasons=reasons,
            gpu=gpu,
            memory=memory,
            psi=psi,
            cpu=cpu,
            processes=processes,
            runtimes=Runtimes(llama_cpp=llama, vllm=vllm),
            errors=errors,
        )
