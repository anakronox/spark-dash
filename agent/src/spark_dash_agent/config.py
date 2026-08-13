"""Agent configuration, all via environment variables.

`NODE_ID` is intended to be the only value that differs between the three
GX10s — everything else stays byte-identical so adding a node is a file copy.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    node_id: str = "unknown"

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

    log_level: str = "INFO"

    @property
    def llama_router_endpoints(self) -> list[str]:
        return _split(self.llama_router_urls)

    @property
    def llama_metrics_allowlist(self) -> list[str]:
        return _split(self.llama_metrics_routers)

    @property
    def vllm_endpoints(self) -> list[str]:
        return _split(self.vllm_urls)


def _split(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]
