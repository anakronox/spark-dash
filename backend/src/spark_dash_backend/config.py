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

    # THE place the cluster is defined. Comma-separated, e.g.
    #   SPARK_NODES=gx10-1=192.168.50.61,gx10-2=192.168.50.62
    # The backend renders Prometheus's target files from this, so a node is
    # added in exactly one place rather than in both a compose file and a
    # hand-maintained YAML inventory.
    spark_nodes: str = ""

    agent_port: int = 9500
    node_exporter_port: int = 9100

    # Where to render Prometheus's file_sd targets. Prometheus re-reads these
    # on its own refresh interval, so adding a node needs no Prometheus restart.
    prometheus_targets_dir: Path | None = Path("/etc/prometheus/targets")

    # Fallback for hand-managed target files; ignored when SPARK_NODES is set.
    agent_targets_file: Path = Path("/etc/prometheus/targets/agents.yml")
    inventory_ttl_s: float = 30.0

    # Live-view cadence. Fast enough to replace a TUI, slow enough that polling
    # three nodes costs nothing. Only runs while a client is subscribed.
    live_poll_interval_s: float = 2.0
    agent_timeout_s: float = 3.0

    # Built Svelte assets. Absent in development, where Vite serves them.
    static_dir: Path = Path("/app/static")

    log_level: str = "INFO"
