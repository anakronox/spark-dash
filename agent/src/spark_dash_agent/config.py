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
    # Set False if a router build turns out to autoload from the discovery path
    # too — keeps the loaded-model list without ever fetching /metrics.
    llama_scrape_loaded_model_metrics: bool = True

    # Comma-separated vLLM /metrics endpoints on this node.
    vllm_urls: str = ""

    log_level: str = "INFO"

    @property
    def llama_router_endpoints(self) -> list[str]:
        return _split(self.llama_router_urls)

    @property
    def vllm_endpoints(self) -> list[str]:
        return _split(self.vllm_urls)


def _split(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]
