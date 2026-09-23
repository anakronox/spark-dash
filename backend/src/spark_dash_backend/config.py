"""Backend configuration, all via environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    host: str = "0.0.0.0"  # noqa: S104 — container-internal; published per compose
    port: int = 8080

    # Baked into the image at build time from the git sha. "unknown" when
    # running from source, which is correct — there's no commit to name.
    backend_version: str = "unknown"

    prometheus_url: str = "http://prometheus:9090"
    prometheus_timeout_s: float = 10.0

    alertmanager_url: str = "http://alertmanager:9093"
    alertmanager_timeout_s: float = 5.0

    # ---- DGX OS updates (roadmap AL) ----
    #
    # This process holds the key and opens the SSH sessions; there is no second
    # container. OFF unless a key is mounted and a login is named, which is
    # what makes the compose overlay the deployment-level knob (AL5) -- leave
    # the overlay out and these stay empty and nothing changes.
    #
    # Until AL3g there was also FLEET_UPDATES_URL, which pointed at a separate
    # spark-fleet-updates container and took precedence over all of this. It
    # was the rollback while the cutover was new, and it went on 2026-09-23
    # once a real update had run embedded.
    fleet_ssh_key: Path | None = None
    fleet_ssh_user: str = ""
    # Its own directory, not the stack root: mounting it must not also expose
    # .env to the container. Holds the fleet list, posture, run records and the
    # known_hosts file that uid 10002 has nowhere else to put (AL4.3).
    fleet_state_dir: Path = Path("/data/fleet")
    fleet_interval_min: float = 60.0
    # The LAN-only opt-out, and the operator's to make: accept a sudo password
    # over plain HTTP. Unset, Update works through the tunnel and is refused on
    # the LAN, which is the fleet service's own rule and not ours to relax.
    fleet_allow_plain_password: bool = False

    @field_validator("fleet_allow_plain_password", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        """An empty string is "not set", not a parse error.

        `${VAR:-}` in a compose file is the ordinary way to say "pass this
        through if the operator set it", and it hands the container an EMPTY
        STRING rather than nothing at all. Pydantic cannot read "" as a bool,
        so the backend died at import -- restart loop, whole dashboard down,
        for an optional feature's opt-out that nobody had set.

        Found in production on 2026-09-22 during the AL cutover. Any bool
        settings added later want this too.
        """
        return False if isinstance(v, str) and not v.strip() else v

    # THE place the cluster is defined. Comma-separated, e.g.
    #   SPARK_NODES=gx10-1=192.168.50.61,gx10-2=192.168.50.62
    # The backend renders Prometheus's target files from this, so a node is
    # added in exactly one place rather than in both a compose file and a
    # hand-maintained YAML inventory.
    spark_nodes: str = ""

    # The cluster definition — nodes, groups, and what each one serves.
    #
    # In the stack directory but gitignored, deliberately: it is live
    # config like `.env`, and a future UI that retires a decommissioned
    # endpoint has to be able to write it without touching git. An example is
    # committed; this file is not.
    #
    # Absent means this deployment still uses SPARK_NODES, which keeps working.
    cluster_config: Path = Path("/etc/spark-dash/cluster.yml")

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
