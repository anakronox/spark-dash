"""Agent configuration, all via environment variables.

Nothing here needs to differ between the three GX10s. `NODE_ID` defaults to the
host's own hostname, so one stack config deploys unchanged to every node —
which is what lets a single stack repo serve the whole cluster instead of one
repo (or one overridden variable) per node.
"""

from __future__ import annotations

import logging
import socket
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from spark_dash_common.thresholds import TEMP_CRITICAL_C, TEMP_WARNING_C, TempThresholds

log = logging.getLogger(__name__)

# Set explicitly rather than left blank, so an unresolvable id is obvious in the
# UI instead of showing as an empty label.
UNKNOWN_NODE_ID = "unknown"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    # Leave unset to use the host's hostname — see `resolve_node_id`. Set it
    # explicitly only if the hostname isn't a good label.
    node_id: str = ""

    # Where the HOST's /proc and /sys are mounted inside the container. This
    # matters: a container's own /proc/pressure/memory and MemAvailable
    # describe the container's limits, not the machine we're monitoring.
    proc_path: Path = Path("/proc")
    sys_path: Path = Path("/sys")

    host: str = "0.0.0.0"  # noqa: S104 — container-internal; published per compose
    port: int = 9500

    gpu_device_index: int = 0

    # Comma-separated. A node commonly runs several router containers; leave
    # blank on a node that serves only vLLM.
    llama_router_urls: str = ""
    llama_router_timeout_s: float = 2.0
    # Routers where per-model `/metrics?model=` requests are permitted.
    # EMPTY BY DEFAULT — that request loads the model on an autoload router, so
    # it is opt-in per router rather than a global switch. Waking a 12B model
    # is a nuisance; waking a 70B one on a shared 128GB pool can exhaust the
    # node. Routers not listed here are still fully visible via /v1/models and
    # /props; only per-model throughput/KV-cache detail is withheld.
    llama_metrics_routers: str = ""

    # Comma-separated vLLM /metrics endpoints on this node.
    vllm_urls: str = ""

    # Temperature bands, overridable per node. The defaults are sparkview's
    # field-validated values, but a node also running sustained image
    # generation legitimately runs hotter — the GX10 sits at ~84C under
    # ComfyUI load without throttling, which would otherwise alert constantly.
    temp_warning_c: float = TEMP_WARNING_C
    temp_critical_c: float = TEMP_CRITICAL_C

    log_level: str = "INFO"

    def resolve_node_id(self) -> str:
        """Work out this node's identity, preferring an explicit NODE_ID.

        The fallback is the HOST's hostname, read from the mounted procfs.
        `socket.gethostname()` inside a container returns the *container's*
        hostname — a random hex id by default — which would make every restart
        look like a brand new node.

        Reading `{proc_path}/sys/kernel/hostname` needs no extra mount: the
        agent already has the host's /proc for PSI and memory.
        """
        explicit = self.node_id.strip()
        if explicit and explicit != UNKNOWN_NODE_ID:
            return explicit

        host_name = _read_host_hostname(self.proc_path)
        if host_name:
            log.info("NODE_ID unset; using host hostname %r", host_name)
            return host_name

        # Container hostname: wrong-ish, but a stable-looking label beats an
        # empty one, and the log line says where it came from.
        fallback = socket.gethostname().strip()
        if fallback:
            log.warning(
                "NODE_ID unset and host hostname unreadable; falling back to "
                "container hostname %r. Set NODE_ID explicitly — a container "
                "hostname changes on every recreate.",
                fallback,
            )
            return fallback

        log.error("could not determine a node id; set NODE_ID explicitly")
        return UNKNOWN_NODE_ID

    @property
    def llama_router_endpoints(self) -> list[str]:
        return _split(self.llama_router_urls)

    @property
    def llama_metrics_allowlist(self) -> list[str]:
        return _split(self.llama_metrics_routers)

    @property
    def temp_thresholds(self) -> TempThresholds:
        return TempThresholds(warning_c=self.temp_warning_c, critical_c=self.temp_critical_c)

    @property
    def vllm_endpoints(self) -> list[str]:
        return _split(self.vllm_urls)


def _read_host_hostname(proc_path: Path) -> str:
    """Read the host's hostname from the mounted procfs.

    `/proc/sys/kernel/hostname` reflects the host even when read through a
    bind-mounted /proc, which is why this needs no extra configuration.
    """
    try:
        return (proc_path / "sys" / "kernel" / "hostname").read_text().strip()
    except OSError:
        return ""


def _split(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]
