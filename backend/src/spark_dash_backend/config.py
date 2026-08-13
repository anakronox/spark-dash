"""Backend configuration, all via environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    host: str = "0.0.0.0"  # noqa: S104 — container-internal; published per compose
    port: int = 8080

    prometheus_url: str = "http://prometheus:9090"
    prometheus_timeout_s: float = 10.0

    # The same file_sd inventory Prometheus reads, so the live view and the
    # history can't disagree about which nodes exist.
    agent_targets_file: Path = Path("/etc/spark-dash/targets/agents.yml")
    inventory_ttl_s: float = 30.0

    # Live-view cadence. Fast enough to replace a TUI, slow enough that polling
    # three nodes costs nothing. Only runs while a client is subscribed.
    live_poll_interval_s: float = 2.0
    agent_timeout_s: float = 3.0

    # Built Svelte assets. Absent in development, where Vite serves them.
    static_dir: Path = Path("/app/static")

    log_level: str = "INFO"
