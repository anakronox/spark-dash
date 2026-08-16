"""The cluster definition — nodes, groups, and what each one serves.

Supersedes `SPARK_NODES`, which could only express ids, hosts and groups. The
runtimes a node serves lived in that node's own `.env`, so the cluster was
defined in two places that did not know about each other — and that split is
the whole reason the per-node stack could not be identical across nodes.

WHERE THIS FILE LIVES MATTERS. It belongs in `central/cluster/` on the
monitoring VM — inside the stack directory but gitignored, with an example
committed, the same pattern `.env` already uses. Letting git track it would
mean a future UI that retires a decommissioned endpoint (roadmap G4) would
have to either edit a tracked file or hold git credentials. Neither is
acceptable, so the placement is a constraint rather than a preference.

PORTS RATHER THAN URLS, by default. A node's runtimes are given as ports and
resolved against that node's own host when served to the agent. Repeating the
IP in every URL would mean changing a node's address touched several lines
instead of one, and it is what made the old per-node `.env` unavoidable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RouterConfig:
    """One llama.cpp router in front of a node's models."""

    url: str
    #: Opt-in for `/metrics?model=`, which resets the router's idle timer and
    #: would otherwise pin a loaded model in memory. Per router, because the
    #: blast radius differs: waking a 12B model is a nuisance, waking a 70B one
    #: on a shared pool can exhaust the node.
    scrape_metrics: bool = False


@dataclass(frozen=True)
class NodeRuntimes:
    """What a node serves. Empty is normal — a node may run neither."""

    llama_routers: list[RouterConfig] = field(default_factory=list)
    vllm: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "llama_routers": [
                {"url": r.url, "scrape_metrics": r.scrape_metrics}
                for r in self.llama_routers
            ],
            "vllm": list(self.vllm),
        }


@dataclass(frozen=True)
class ClusterNode:
    """A node as the cluster file defines it: identity, address, and what it serves."""

    node_id: str
    host: str
    group: str | None = None
    agent_port: int = 9500
    node_exporter_port: int = 9100
    runtimes: NodeRuntimes = field(default_factory=NodeRuntimes)


class ClusterConfigError(ValueError):
    """The file exists but cannot be trusted.

    Raised rather than silently falling back: a malformed cluster definition
    that quietly degrades to "no nodes" would take the whole dashboard dark and
    look like an outage rather than a typo.
    """


def _resolve(entry: object, host: str, *, default_path: str = "") -> str | None:
    """Turn a port or an explicit url into a full endpoint.

    `port: 8001` is the normal form and is what keeps the node stack identical
    everywhere. `url:` stays available for the case the shorthand cannot
    express — a runtime that is not on the node's own address.
    """
    if isinstance(entry, int):
        return f"http://{host}:{entry}{default_path}"
    if isinstance(entry, str):
        stripped = entry.strip()
        if not stripped:
            return None
        if stripped.startswith(("http://", "https://")):
            return stripped
        if stripped.isdigit():
            return f"http://{host}:{stripped}{default_path}"
        return None
    if isinstance(entry, dict):
        if entry.get("url"):
            return str(entry["url"]).strip()
        port = entry.get("port")
        if port is not None:
            return f"http://{host}:{port}{default_path}"
    return None


def parse_runtimes(raw: object, host: str) -> NodeRuntimes:
    if not isinstance(raw, dict):
        return NodeRuntimes()

    routers: list[RouterConfig] = []
    for item in raw.get("llama_routers") or []:
        url = _resolve(item, host)
        if not url:
            log.warning("skipping unparseable llama_router entry: %r", item)
            continue
        scrape = bool(item.get("scrape_metrics")) if isinstance(item, dict) else False
        routers.append(RouterConfig(url=url.rstrip("/"), scrape_metrics=scrape))

    vllm: list[str] = []
    for item in raw.get("vllm") or []:
        # vLLM's Prometheus endpoint is conventionally /metrics, so the port
        # shorthand appends it rather than making every entry spell it out.
        url = _resolve(item, host, default_path="/metrics")
        if not url:
            log.warning("skipping unparseable vllm entry: %r", item)
            continue
        vllm.append(url)

    return NodeRuntimes(llama_routers=routers, vllm=vllm)


def parse_cluster(text: str) -> list[ClusterNode]:
    """Parse the whole cluster: identity, grouping and runtimes together.

    This is deliberately ONE file. Node ids, hosts and groups used to come from
    `SPARK_NODES` while runtimes came from each node's own `.env` — two places
    that did not know about each other, describing one thing. Folding identity
    in here means adding a node is a single edit in a single file, and there is
    no way for the two halves to disagree.
    """
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ClusterConfigError(f"not valid YAML: {exc}") from exc

    if payload is None:
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list):
        raise ClusterConfigError("expected a top-level `nodes:` list")

    out: list[ClusterNode] = []
    seen: set[str] = set()
    for entry in payload["nodes"]:
        if not isinstance(entry, dict):
            raise ClusterConfigError(f"each node must be a mapping, got {type(entry).__name__}")
        node_id = str(entry.get("id") or "").strip()
        host = str(entry.get("host") or "").strip()
        if not node_id:
            raise ClusterConfigError("a node is missing its `id`")
        if not host:
            raise ClusterConfigError(f"node {node_id!r} is missing its `host`")
        if node_id in seen:
            raise ClusterConfigError(f"duplicate node id {node_id!r}")
        seen.add(node_id)

        # Grouping is not cosmetic: clustered nodes pool memory for distributed
        # inference, so capacity sums WITHIN a group and never across groups.
        raw_group = entry.get("group")
        group = str(raw_group).strip() or None if raw_group is not None else None

        out.append(
            ClusterNode(
                node_id=node_id,
                host=host,
                group=group,
                agent_port=int(entry.get("agent_port") or 9500),
                node_exporter_port=int(entry.get("node_exporter_port") or 9100),
                runtimes=parse_runtimes(entry.get("runtimes"), host),
            )
        )

    return out


def load_cluster(path: Path) -> list[ClusterNode]:
    """Read the cluster file, or return nothing if it does not exist.

    A missing file is not an error — it means this deployment has not migrated
    from `SPARK_NODES` yet, and the caller falls back. A file that exists but
    is malformed IS an error, because silently treating a typo as "no runtimes
    configured" would leave every node reporting no models with nothing
    explaining why.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ClusterConfigError(f"could not read {path}: {exc}") from exc

    return parse_cluster(text)
