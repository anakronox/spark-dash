"""Which nodes exist.

ONE place defines the cluster: `cluster.yml` on the monitoring VM. The backend
parses it, uses it for live polling, renders Prometheus's `file_sd` target
files from it, AND serves each node its runtimes from the same entries.

That inversion is the point. Prometheus can't read environment variables in its
config, so the obvious alternative is hand-maintained target YAML — which means
the node list exists twice and can drift. When it drifts the failure is quiet
and confusing: a node visible in history but absent from the live view, or the
reverse. Making the backend the source of truth and Prometheus a consumer means
adding a node is one edit in one file.

Two fallbacks remain, in order: `SPARK_NODES` (the previous source of truth,
kept so a deployment that has not migrated keeps working) and reading target
files directly (for anyone who would rather manage them by hand).

`SPARK_NODES` could only ever express id, host and cluster; a node's runtimes
lived in that node's own `.env`. Folding both into `cluster.yml` is what makes
the per-node stack byte-identical, since nothing node-specific is left in it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from spark_dash_backend.cluster import ClusterConfigError, authority, load_cluster

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

    # Nodes clustered together pool their memory for distributed inference, so
    # a model can span the cluster. `None` means the node stands alone.
    #
    # A NAME, never a count: "pair" is wrong the moment a third node joins, and
    # clusters in the wild run to 32. It is also a Prometheus label and a UI
    # heading, so it has to be readable on its own.
    #
    # This is what makes capacity arithmetic correct. Summing free memory
    # within a cluster is real; summing across clusters describes capacity that
    # doesn't exist, because a model can't span machines that aren't clustered.
    cluster: str | None = None

    @property
    def cluster_key(self) -> str:
        """Aggregation key. A standalone node is a cluster of one, so callers
        can aggregate uniformly instead of special-casing."""
        return self.cluster or self.node_id

    @property
    def standalone(self) -> bool:
        return self.cluster is None

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

        gx10-1=192.168.50.61          standalone node, explicit id (preferred)
        gx10-1@192.168.50.61          same, alternative separator
        gx10-1=192.168.50.61:9500     explicit port
        192.168.50.61                 id derived from the host
        alpha/gx10-2=192.168.50.62    node in the cluster "alpha"

    A `cluster/` prefix marks nodes that are clustered together and pool memory
    for distributed inference. Nodes without one stand alone. Not every node is
    part of a cluster, and treating them as one would misreport capacity.

    The prefix is a NAME, not a count — "pair" stops being true the moment a
    third node joins.

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

        label = ""
        target = entry
        for sep in ("=", "@"):
            if sep in entry:
                label, _, target = entry.partition(sep)
                label, target = label.strip(), target.strip()
                break

        # A "cluster/" prefix may appear on either side of the separator, since
        # `alpha/192.168.50.62` (no explicit id) is a reasonable thing to write.
        cluster: str | None = None
        if "/" in label:
            cluster, _, label = label.partition("/")
            cluster, label = cluster.strip() or None, label.strip()
        elif "/" in target:
            cluster, _, target = target.partition("/")
            cluster, target = cluster.strip() or None, target.strip()

        node_id = label

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
                cluster=cluster,
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

        # The cluster label must be read back, not just written. Dropping it
        # here made clustered nodes look standalone on the file-based path, and
        # the failure is silent and wrong in the dangerous direction: capacity
        # arithmetic would stop pooling their memory and under-report what the
        # cluster can actually hold, so a model that would fit looks like it
        # won't.
        #
        # `group` is the old label name, still read so target files written by
        # an older backend keep working.
        raw_cluster = labels.get("cluster", labels.get("group"))
        cluster = str(raw_cluster).strip() or None if raw_cluster is not None else None

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
                    cluster=cluster,
                )
            )

    return nodes


def render_file_sd(nodes: list[Node], *, port_of, header: str) -> str:
    """Render nodes as Prometheus `file_sd` YAML.

    The `node` label is written explicitly so a target that is DOWN still has
    an identity — otherwise a node that never came up would be invisible in
    Prometheus rather than visibly missing.

    `cluster` is written too, so history can be aggregated the same way the
    live view does: `sum by (cluster)` is meaningful, `sum` across everything
    is not.
    """
    entries = []
    for node in nodes:
        labels = {"node": node.node_id}
        if node.cluster:
            labels["cluster"] = node.cluster
        entries.append({"targets": [port_of(node)], "labels": labels})
    body = yaml.safe_dump(entries, default_flow_style=False, sort_keys=False)
    return f"{header}\n{body}"


def render_vllm_file_sd(cluster_nodes, *, header: str) -> str:
    """Render every configured vLLM endpoint as Prometheus `file_sd` YAML.

    ONE ENTRY PER ENDPOINT, not per node: a node may serve several vLLM
    instances, which is why this cannot reuse `render_file_sd` — that renders
    one target per node.

    WHY THIS IS GENERATED AT ALL. It was hand-maintained in
    `config/vllm-targets.yml`, which made cluster.yml and that file two
    independent sources for one fact. Retiring an endpoint from the dashboard
    removed it from cluster.yml, so the AGENT stopped polling it — and
    Prometheus, reading the other file, carried on scraping. The banner
    correctly came back and the button looked broken. One source removes the
    class of bug rather than the instance.

    Reduced to `host:port`: the config holds a scrape URL with a path, and
    Prometheus wants an authority.
    """
    entries = []
    for node in cluster_nodes:
        for url in node.runtimes.vllm:
            labels = {"node": node.node_id}
            if node.cluster:
                labels["cluster"] = node.cluster
            entries.append({"targets": [authority(url)], "labels": labels})
    body = yaml.safe_dump(entries, default_flow_style=False, sort_keys=False)
    return f"{header}\n{body}"


def _generated_header(source: str) -> str:
    """Name the file's real source, so an operator editing the wrong thing
    finds out from the file itself rather than from a change that never
    takes."""
    origin = {
        "cluster.yml": "cluster.yml on the monitoring VM",
        "env": "the SPARK_NODES environment variable in central/.env",
    }.get(source, "the Prometheus target files themselves")
    return (
        "# GENERATED FILE — do not edit.\n"
        f"# Rendered by spark-dash-backend from {origin}.\n"
        "# To add or remove a node, edit that source; Prometheus picks the\n"
        "# change up on its next file_sd refresh without a restart of its own.\n"
    )


def write_prometheus_targets(
    nodes: list[Node],
    targets_dir: Path,
    *,
    source: str = "cluster.yml",
    cluster_nodes=None,
) -> bool:
    """Write the target files Prometheus reads. Returns True if anything changed.

    Writes only on change so Prometheus isn't re-reading identical files, and
    so the mtime is a real signal of when the inventory last moved.
    """
    header = _generated_header(source)
    files = {
        "agents.yml": render_file_sd(nodes, port_of=lambda n: n.address, header=header),
        "node-exporters.yml": render_file_sd(
            nodes, port_of=lambda n: n.node_exporter_address, header=header
        ),
    }
    # Only when the cluster file is the source. Under SPARK_NODES there are no
    # runtimes to render, and writing an empty list would silently retire every
    # vLLM target the moment someone fell back to env.
    if cluster_nodes is not None:
        files["vllm.yml"] = render_vllm_file_sd(cluster_nodes, header=header)

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
        cluster_config: Path | None = None,
        nodes_env: str = "",
        targets_file: Path | None = None,
        prometheus_targets_dir: Path | None = None,
        agent_port: int = DEFAULT_AGENT_PORT,
        node_exporter_port: int = DEFAULT_NODE_EXPORTER_PORT,
        ttl_s: float = 30.0,
    ) -> None:
        self._cluster_config = cluster_config
        self._nodes_env = nodes_env
        self._targets_file = targets_file
        self._prometheus_targets_dir = prometheus_targets_dir
        self._agent_port = agent_port
        self._node_exporter_port = node_exporter_port
        self._ttl_s = ttl_s

        self._nodes: list[Node] = []
        #: The cluster file's own entries, kept because `Node` deliberately
        #: carries only identity and ports — the runtimes it drops are exactly
        #: what the vLLM scrape targets are rendered from.
        self._cluster: list = []
        self._loaded_at = 0.0

    @property
    def source(self) -> str:
        if self._cluster_config and self._cluster_config.exists():
            return "cluster.yml"
        return "env" if self._nodes_env.strip() else "file"

    def nodes(self, now: float | None = None) -> list[Node]:
        now = time.monotonic() if now is None else now
        if not self._nodes or (now - self._loaded_at) >= self._ttl_s:
            self._nodes = self._load()
            self._loaded_at = now
        return self._nodes

    def invalidate(self) -> None:
        """Force the next `nodes()` to re-read.

        The TTL exists so a busy dashboard is not stat-ing a file every poll.
        After the config is edited that caching becomes a lie: the save looks
        like it failed for up to the TTL, and the user edits again.
        """
        self._loaded_at = 0.0

    def sync_prometheus_targets(self) -> bool:
        """Render the current inventory into Prometheus's target directory."""
        if self._prometheus_targets_dir is None:
            return False
        # `nodes()` first: it refreshes the cache that `_cluster` is filled
        # from, so asking in the other order would render last cycle's vLLM.
        rendered = self.nodes()
        return write_prometheus_targets(
            rendered,
            self._prometheus_targets_dir,
            source=self.source,
            cluster_nodes=self._cluster,
        )

    def _load(self) -> list[Node]:
        # cluster.yml first: it is the one place the cluster is defined, and it
        # carries identity, clustering AND runtimes together. SPARK_NODES could
        # only express the first two, leaving runtimes in each node's own .env
        # — two files describing one thing, with no way to keep them agreeing.
        if self._cluster_config is not None:
            try:
                cluster = load_cluster(self._cluster_config)
            except ClusterConfigError:
                # Keep serving the previous inventory rather than dropping every
                # node because of a typo. Loud, because the alternative is a
                # dashboard that silently reverts to an older cluster.
                log.exception(
                    "cluster config at %s is invalid; keeping the previous "
                    "inventory. Fix the file — nothing will pick up changes "
                    "until it parses.",
                    self._cluster_config,
                )
                return self._nodes
            if cluster:
                # Kept alongside, because `Node` drops the runtimes and those
                # are what the vLLM scrape targets are rendered from.
                self._cluster = cluster
                return [
                    Node(
                        node_id=c.node_id,
                        host=c.host,
                        agent_port=c.agent_port,
                        node_exporter_port=c.node_exporter_port,
                        cluster=c.cluster,
                    )
                    for c in cluster
                ]

        # Past this point the cluster file is not in play, so any retained
        # entries would describe a source no longer being used.
        self._cluster = []

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
