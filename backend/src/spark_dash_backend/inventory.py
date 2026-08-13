"""Which nodes exist.

ONE place defines the cluster: the `SPARK_NODES` environment variable. The
backend parses it, uses it for live polling, and renders Prometheus's
`file_sd` target files from it.

That inversion is the point. Prometheus can't read environment variables in its
config, so the obvious alternative is hand-maintained target YAML — which means
the node list exists twice and can drift. When it drifts the failure is quiet
and confusing: a node visible in history but absent from the live view, or the
reverse. Making the backend the source of truth and Prometheus a consumer means
adding a node is one edit in one file.

Reading target files directly is still supported as a fallback, for anyone who
would rather manage them by hand.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

DEFAULT_AGENT_PORT = 9500
DEFAULT_NODE_EXPORTER_PORT = 9100


@dataclass(frozen=True)
class Node:
    """One GX10, as both a scrape target and a live-poll target."""

    node_id: str
    host: str
    agent_port: int = DEFAULT_AGENT_PORT
    node_exporter_port: int = DEFAULT_NODE_EXPORTER_PORT

    @property
    def address(self) -> str:
        return f"{self.host}:{self.agent_port}"

    @property
    def node_exporter_address(self) -> str:
        return f"{self.host}:{self.node_exporter_port}"

    @property
    def agent_url(self) -> str:
        return f"http://{self.address}"

    @property
    def snapshot_url(self) -> str:
        return f"{self.agent_url}/snapshot"


def parse_nodes_env(
    value: str,
    *,
    agent_port: int = DEFAULT_AGENT_PORT,
    node_exporter_port: int = DEFAULT_NODE_EXPORTER_PORT,
) -> list[Node]:
    """Parse `SPARK_NODES` into nodes.

    Accepted forms, comma-separated:

        gx10-1=192.168.50.61          explicit id (preferred)
        gx10-1@192.168.50.61          same, alternative separator
        gx10-1=192.168.50.61:9500     explicit port
        192.168.50.61                 id derived from the host

    Explicit ids are preferred because the id becomes the `node` label on every
    metric — deriving it from an IP means changing that IP silently splits the
    node's history in two.
    """
    nodes: list[Node] = []
    seen: set[str] = set()

    for raw in value.split(","):
        entry = raw.strip()
        if not entry:
            continue

        node_id = ""
        target = entry
        for sep in ("=", "@"):
            if sep in entry:
                node_id, _, target = entry.partition(sep)
                node_id, target = node_id.strip(), target.strip()
                break

        if not target:
            log.warning("skipping node entry with no host: %r", entry)
            continue

        host, port = _split_host_port(target, agent_port)
        if not host:
            log.warning("skipping node entry with no host: %r", entry)
            continue

        node_id = node_id or host
        if node_id in seen:
            log.warning("duplicate node id %r; ignoring %r", node_id, entry)
            continue
        seen.add(node_id)

        nodes.append(
            Node(
                node_id=node_id,
                host=host,
                agent_port=port,
                node_exporter_port=node_exporter_port,
            )
        )

    return nodes


def _split_host_port(target: str, default_port: int) -> tuple[str, int]:
    host, sep, port_text = target.rpartition(":")
    if not sep:
        return target, default_port
    try:
        return host, int(port_text)
    except ValueError:
        # Not a port — probably a bare hostname containing a colon.
        log.warning("unparseable port in %r; using default %d", target, default_port)
        return target, default_port


def parse_file_sd(
    content: str, *, node_exporter_port: int = DEFAULT_NODE_EXPORTER_PORT
) -> list[Node]:
    """Parse Prometheus `file_sd` YAML into nodes (fallback path).

    Malformed entries are skipped with a warning rather than raising — one bad
    line shouldn't blind the whole dashboard.
    """
    try:
        payload = yaml.safe_load(content)
    except yaml.YAMLError:
        log.exception("inventory is not valid YAML")
        return []

    if not isinstance(payload, list):
        return []

    nodes: list[Node] = []
    seen: set[str] = set()

    for entry in payload:
        if not isinstance(entry, dict):
            continue
        targets = entry.get("targets") or []
        labels = entry.get("labels") or {}
        if not isinstance(targets, list) or not isinstance(labels, dict):
            log.warning("skipping malformed inventory entry: %r", entry)
            continue

        for target in targets:
            if not isinstance(target, str) or not target.strip():
                continue
            host, port = _split_host_port(target.strip(), DEFAULT_AGENT_PORT)
            node_id = str(labels.get("node") or host)

            if node_id in seen:
                log.warning("duplicate node id %r in inventory", node_id)
                continue
            seen.add(node_id)
            nodes.append(
                Node(
                    node_id=node_id,
                    host=host,
                    agent_port=port,
                    node_exporter_port=node_exporter_port,
                )
            )

    return nodes


def render_file_sd(nodes: list[Node], *, port_of, header: str) -> str:
    """Render nodes as Prometheus `file_sd` YAML.

    The `node` label is written explicitly so a target that is DOWN still has
    an identity — otherwise a node that never came up would be invisible in
    Prometheus rather than visibly missing.
    """
    entries = [
        {"targets": [port_of(node)], "labels": {"node": node.node_id}} for node in nodes
    ]
    body = yaml.safe_dump(entries, default_flow_style=False, sort_keys=False)
    return f"{header}\n{body}"


_GENERATED_HEADER = (
    "# GENERATED FILE — do not edit.\n"
    "# Rendered by spark-dash-backend from the SPARK_NODES environment\n"
    "# variable. To add or remove a node, edit SPARK_NODES in deploy/central/.env\n"
    "# and restart the backend; Prometheus picks the change up on its next\n"
    "# file_sd refresh without a restart of its own.\n"
)


def write_prometheus_targets(nodes: list[Node], targets_dir: Path) -> bool:
    """Write the target files Prometheus reads. Returns True if anything changed.

    Writes only on change so Prometheus isn't re-reading identical files, and
    so the mtime is a real signal of when the inventory last moved.
    """
    files = {
        "agents.yml": render_file_sd(
            nodes, port_of=lambda n: n.address, header=_GENERATED_HEADER
        ),
        "node-exporters.yml": render_file_sd(
            nodes, port_of=lambda n: n.node_exporter_address, header=_GENERATED_HEADER
        ),
    }

    changed = False
    for name, content in files.items():
        path = targets_dir / name
        try:
            if path.exists() and path.read_text() == content:
                continue
            path.write_text(content)
            changed = True
            log.info("wrote %s (%d node(s))", path, len(nodes))
        except OSError:
            # Not fatal: the live view still works, and Prometheus keeps
            # whatever targets it already had.
            log.warning("could not write %s — is the volume writable?", path, exc_info=True)

    return changed


class Inventory:
    """The cluster's node list, from env or file, cached with a TTL."""

    def __init__(
        self,
        *,
        nodes_env: str = "",
        targets_file: Path | None = None,
        prometheus_targets_dir: Path | None = None,
        agent_port: int = DEFAULT_AGENT_PORT,
        node_exporter_port: int = DEFAULT_NODE_EXPORTER_PORT,
        ttl_s: float = 30.0,
    ) -> None:
        self._nodes_env = nodes_env
        self._targets_file = targets_file
        self._prometheus_targets_dir = prometheus_targets_dir
        self._agent_port = agent_port
        self._node_exporter_port = node_exporter_port
        self._ttl_s = ttl_s

        self._nodes: list[Node] = []
        self._loaded_at = 0.0

    @property
    def source(self) -> str:
        return "env" if self._nodes_env.strip() else "file"

    def nodes(self, now: float | None = None) -> list[Node]:
        now = time.monotonic() if now is None else now
        if not self._nodes or (now - self._loaded_at) >= self._ttl_s:
            self._nodes = self._load()
            self._loaded_at = now
        return self._nodes

    def sync_prometheus_targets(self) -> bool:
        """Render the current inventory into Prometheus's target directory."""
        if self._prometheus_targets_dir is None:
            return False
        return write_prometheus_targets(self.nodes(), self._prometheus_targets_dir)

    def _load(self) -> list[Node]:
        if self._nodes_env.strip():
            nodes = parse_nodes_env(
                self._nodes_env,
                agent_port=self._agent_port,
                node_exporter_port=self._node_exporter_port,
            )
            if not nodes:
                log.warning("SPARK_NODES is set but yielded no nodes: %r", self._nodes_env)
            return nodes

        if self._targets_file is None:
            return []

        try:
            content = self._targets_file.read_text()
        except OSError:
            # Keep serving the previous inventory rather than dropping every
            # node because the file was briefly unreadable mid-edit.
            log.warning("could not read inventory at %s", self._targets_file)
            return self._nodes

        nodes = parse_file_sd(content, node_exporter_port=self._node_exporter_port)
        if not nodes:
            log.warning("inventory at %s yielded no nodes", self._targets_file)
        return nodes
