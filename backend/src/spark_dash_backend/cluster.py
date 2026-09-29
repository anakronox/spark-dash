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

import contextlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml
from spark_dash_common.models import ENGINE_RUNTIMES

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
    #: runtime name -> /metrics endpoints, for the engines that are just a
    #: list of endpoints (`ENGINE_RUNTIMES`). A mapping rather than a field
    #: per engine so parsing, rendering, serving and retiring an endpoint are
    #: each written once — the per-engine spelling stays in the YAML and in
    #: the snapshot, where it is what an operator and an alert rule read.
    engines: dict[str, list[str]] = field(default_factory=dict)

    @property
    def vllm(self) -> list[str]:
        """The engine every existing deployment already has."""
        return self.engines.get("vllm", [])

    def as_dict(self) -> dict:
        """What the agent is served. Every engine key is present, empty
        included: an absent key and an empty list would otherwise be
        indistinguishable to an agent deciding whether an engine is configured
        at all, and that distinction is what the unmonitored-runtime warning
        rests on."""
        return {
            "llama_routers": [
                {"url": r.url, "scrape_metrics": r.scrape_metrics}
                for r in self.llama_routers
            ],
            **{runtime: list(self.engines.get(runtime, [])) for runtime in ENGINE_RUNTIMES},
        }


@dataclass(frozen=True)
class InterfacePolicy:
    """Which of a node's network interfaces are excluded from alerting.

    AN IGNORE LIST, not an allowlist. Every interface is watched unless named
    here, so an interface nobody has configured still alerts — forgetting to
    maintain this makes the dashboard noisy, never silent, which is the safe
    direction for a system whose recurring failure mode is silence. It also
    keeps A4's property that a newly cabled port is watched from the moment it
    comes up, with nothing to remember.

    Names are matched exactly against what the node reports. A name that
    matches nothing is kept rather than dropped: an interface can be absent
    because the node is down or the NIC was renamed, and silently discarding
    the entry would quietly re-arm an alert someone deliberately turned off.
    """

    ignore: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"ignore": list(self.ignore)}


@dataclass(frozen=True)
class ClusterNode:
    """A node as the cluster file defines it: identity, address, and what it serves."""

    node_id: str
    host: str
    cluster: str | None = None
    agent_port: int = 9500
    node_exporter_port: int = 9100
    runtimes: NodeRuntimes = field(default_factory=NodeRuntimes)
    interfaces: InterfacePolicy = field(default_factory=InterfacePolicy)


class ClusterConfigError(ValueError):
    """The file exists but cannot be trusted.

    Raised rather than silently falling back: a malformed cluster definition
    that quietly degrades to "no nodes" would take the whole dashboard dark and
    look like an outage rather than a typo.
    """


def authority(url: str) -> str:
    """host:port from a URL — how Prometheus names an instance.

    Shared because two places must agree on it: the file_sd renderer, which
    turns a configured URL into a scrape target, and the retire endpoint, which
    matches a Prometheus instance back to a config entry. If they disagreed,
    retire would silently match nothing and the target would come straight
    back.
    """
    parsed = urlparse(url if "//" in url else f"//{url}")
    return parsed.netloc or url


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

    engines: dict[str, list[str]] = {}
    for runtime in ENGINE_RUNTIMES:
        urls: list[str] = []
        for item in raw.get(runtime) or []:
            # Both engines expose Prometheus on /metrics conventionally, so
            # the port shorthand appends it rather than making every entry
            # spell it out.
            url = _resolve(item, host, default_path="/metrics")
            if not url:
                log.warning("skipping unparseable %s entry: %r", runtime, item)
                continue
            urls.append(url)
        if urls:
            engines[runtime] = urls

    return NodeRuntimes(llama_routers=routers, engines=engines)


def parse_interfaces(raw: object) -> InterfacePolicy:
    """Parse `interfaces: {ignore: [...]}`.

    Tolerant of a bare list — `interfaces: [enP2p1s0f1np1]` is what someone
    writes from memory, and reading it as the ignore list is the only sensible
    meaning it could have. Anything else yields the empty policy, which watches
    everything: a malformed entry must not silently disarm alerting.
    """
    if isinstance(raw, dict):
        entries = raw.get("ignore")
    elif isinstance(raw, list):
        entries = raw
    else:
        if raw is not None:
            log.warning("ignoring unparseable `interfaces:` block: %r", raw)
        return InterfacePolicy()

    names = []
    for item in entries or []:
        name = str(item).strip()
        if name:
            names.append(name)
    return InterfacePolicy(ignore=names)


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

        # Not cosmetic: clustered nodes pool memory for distributed inference,
        # so capacity sums WITHIN a cluster and never across clusters.
        #
        # `group:` is the old spelling. Still accepted, but LOUDLY — silently
        # ignoring it would drop the node to standalone, and that breaks
        # capacity arithmetic in the dangerous direction: free memory stops
        # pooling, so a model that would fit reads as one that won't.
        raw_cluster = entry.get("cluster")
        if raw_cluster is None and entry.get("group") is not None:
            raw_cluster = entry.get("group")
            log.warning(
                "node %r uses `group:`, which was renamed to `cluster:`. Still "
                "honoured, but rename it — a cluster is named, not sized, and "
                "`group` will stop being read.",
                node_id,
            )
        cluster = str(raw_cluster).strip() or None if raw_cluster is not None else None

        out.append(
            ClusterNode(
                node_id=node_id,
                host=host,
                cluster=cluster,
                agent_port=int(entry.get("agent_port") or 9500),
                node_exporter_port=int(entry.get("node_exporter_port") or 9100),
                runtimes=parse_runtimes(entry.get("runtimes"), host),
                interfaces=parse_interfaces(entry.get("interfaces")),
            )
        )

    return out


PRIVATE_HOST_HINT = (
    "hosts must be private (RFC1918, loopback, link-local) or a hostname; "
    "public IP literals are refused"
)


def _host_is_acceptable(host: str) -> bool:
    """Reject public IP literals, allow private ones and hostnames.

    NOT the primary control — the dashboard sits behind OAuth at the tunnel
    edge, and this is defence in depth behind it. But the agent polls whatever
    ends up in `llama_routers`, so a config write is a request-forgery
    primitive aimed at the LAN, and narrowing the value space costs nothing:
    every real deployment of this points at private addresses anyway.

    Hostnames are allowed because they cannot be judged without resolving them,
    and resolution here would be a different (and worse) kind of trust.
    """
    import ipaddress

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return bool(host) and " " not in host  # a hostname
    return ip.is_private or ip.is_loopback or ip.is_link_local


def validate_cluster(nodes: list[ClusterNode]) -> None:
    """Raise ClusterConfigError if this could not safely be written.

    Checked here rather than at the API boundary so every writer gets the same
    rules, including any future one.
    """
    seen: set[str] = set()
    for n in nodes:
        if not n.node_id.strip():
            raise ClusterConfigError("every node needs an id")
        if n.node_id in seen:
            raise ClusterConfigError(f"duplicate node id {n.node_id!r}")
        seen.add(n.node_id)
        if not n.host.strip():
            raise ClusterConfigError(f"node {n.node_id!r} needs a host")
        if not _host_is_acceptable(n.host):
            raise ClusterConfigError(f"node {n.node_id!r}: {PRIVATE_HOST_HINT}")
        for port in (n.agent_port, n.node_exporter_port):
            if not 1 <= port <= 65535:
                raise ClusterConfigError(f"node {n.node_id!r}: port {port} out of range")


def dump_cluster(nodes: list[ClusterNode], *, preserve: Mapping | None = None) -> str:
    """Render the cluster back to YAML.

    `preserve` is the file's current top-level mapping. Every key in it other
    than `nodes` is written back unchanged and in its original place; `nodes`
    is replaced by `nodes`. See `write_cluster`.

    PORTS, NOT URLS. A runtime on the node's own host is written as a port, so
    changing a node's address stays one edit — and it is what keeps a UI write
    from being able to name an arbitrary URL at all. A runtime that genuinely
    lives elsewhere keeps its explicit url.

    COMMENTS DO NOT SURVIVE. This serialises the parsed model, so any hand
    written notes in the file are lost the first time the dashboard writes it.
    Said in the header rather than discovered, with a pointer to the example
    file that carries the documentation.
    """
    out: list[dict] = []
    for n in nodes:
        entry: dict = {"id": n.node_id, "host": n.host}
        if n.cluster:
            entry["cluster"] = n.cluster
        if n.agent_port != 9500:
            entry["agent_port"] = n.agent_port
        if n.node_exporter_port != 9100:
            entry["node_exporter_port"] = n.node_exporter_port

        runtimes: dict = {}
        routers = []
        for r in n.runtimes.llama_routers:
            port = _own_port(r.url, n.host)
            item: dict = {"port": port} if port else {"url": r.url}
            if r.scrape_metrics:
                item["scrape_metrics"] = True
            routers.append(item)
        if routers:
            runtimes["llama_routers"] = routers
        for runtime in ENGINE_RUNTIMES:
            urls = n.runtimes.engines.get(runtime) or []
            entries = [_own_port(u, n.host, "/metrics") or u for u in urls]
            if entries:
                runtimes[runtime] = entries
        if runtimes:
            entry["runtimes"] = runtimes
        if n.interfaces.ignore:
            entry["interfaces"] = {"ignore": list(n.interfaces.ignore)}
        out.append(entry)

    doc: dict = {}
    for key, value in (preserve or {}).items():
        doc[key] = out if key == "nodes" else value
    doc.setdefault("nodes", out)
    body = yaml.safe_dump(doc, default_flow_style=False, sort_keys=False)
    return (
        "# The cluster — nodes and what each one serves.\n"
        "#\n"
        "# MANAGED BY THE DASHBOARD. Written whenever the cluster is edited in\n"
        "# settings, which means hand-written comments here are NOT preserved.\n"
        "# The documented reference, with every option explained, is\n"
        "# central/cluster.yml.example in the repo.\n"
        "#\n"
        "# Editing this file by hand still works and is picked up on the\n"
        "# backend's next read; the dashboard re-reads before it writes, so a\n"
        "# hand edit is not silently clobbered.\n"
        f"{body}"
    )


def _own_port(url: str, host: str, suffix: str = "") -> int | None:
    """The port, if this url is just `http://<this node>:<port><suffix>`."""
    prefix = f"http://{host}:"
    if not url.startswith(prefix):
        return None
    rest = url[len(prefix) :]
    if suffix:
        if not rest.endswith(suffix):
            return None
        rest = rest[: -len(suffix)]
    return int(rest) if rest.isdigit() else None


def write_cluster(path: Path, nodes: list[ClusterNode]) -> None:
    """Validate and write atomically.

    Temp file in the SAME directory then os.replace, because the container
    mounts the parent as a directory and a rename is resolved on next access —
    a partial write would otherwise be read as a truncated cluster and take
    every node dark. The same reason the mount is a directory and not a file.
    """
    validate_cluster(nodes)
    # READ-MODIFY-WRITE. This module owns `nodes` and nothing else in the file.
    # Writing only what it owns deleted every other top-level block, so the
    # first node saved from Settings would have silently removed the gateway
    # config (roadmap AM2a). A file that is not YAML has nothing to keep, and
    # is replaced rather than refused, since refusing would leave Settings
    # unable to repair it.
    text = dump_cluster(nodes, preserve=_current_document(path))
    _atomic_write(path, text)


def _atomic_write(path: Path, text: str) -> None:
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)

    # Re-parse what we are about to write. Writing something we cannot read
    # back would be the one unrecoverable outcome here.
    parse_cluster(text)

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cluster.", suffix=".yml")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# --- gateway (roadmap AM2) ---------------------------------------------------
#
# A LiteLLM gateway in front of the engines, for client stats. It lives in this
# file because this is the one server-side config the backend already writes,
# but it is parsed SEPARATELY from the nodes: a typo in `gateway:` must never
# take a node off the dashboard.


@dataclass(frozen=True)
class GatewayConfig:
    """Where the gateway is, and whether its stats are wanted.

    `url` is always a base, `scheme://host[:port]`; `parse_gateway` strips the
    `/v1` a client URL carries, since that is what gets pasted.
    """

    url: str
    enabled: bool = True

    @property
    def authority(self) -> str:
        """`host:port`, as a Prometheus target."""
        return urlparse(self.url).netloc

    @property
    def scheme(self) -> str:
        return urlparse(self.url).scheme


def normalize_gateway_url(raw: object) -> str:
    """`http://host:4000/v1/` -> `http://host:4000`, or a ClusterConfigError
    saying what is wrong with it."""
    hint = "give the gateway's address, like http://<gateway-host>:4000"
    if not isinstance(raw, str) or not raw.strip():
        raise ClusterConfigError(f"gateway.litellm.url is missing; {hint}")
    parsed = urlparse(raw.strip())
    try:
        parsed.port  # noqa: B018 — raises on a non-numeric or out-of-range port
    except ValueError as exc:
        raise ClusterConfigError(f"gateway.litellm.url has a bad port: {raw!r}") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ClusterConfigError(f"gateway.litellm.url is not an http(s) URL: {raw!r}; {hint}")
    if parsed.username or parsed.password:
        # This file is served to the UI and written by it. A key belongs in the
        # gateway's own config, not in an address the dashboard displays.
        raise ClusterConfigError("gateway.litellm.url must not carry credentials")
    if parsed.query or parsed.fragment or parsed.path.rstrip("/") not in ("", "/v1"):
        raise ClusterConfigError(
            f"gateway.litellm.url should be the gateway itself, not a path on it: {raw!r}. "
            "A trailing /v1 is fine."
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def parse_gateway(payload: object) -> GatewayConfig | None:
    """The `gateway.litellm` block, or None when there is none."""
    if not isinstance(payload, dict) or payload.get("gateway") is None:
        return None
    block = payload["gateway"]
    if not isinstance(block, dict):
        raise ClusterConfigError("`gateway:` must be a mapping")
    litellm = block.get("litellm")
    if litellm is None:
        return None
    if not isinstance(litellm, dict):
        raise ClusterConfigError("`gateway.litellm:` must be a mapping with a `url:`")
    enabled = litellm.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ClusterConfigError(f"gateway.litellm.enabled must be true or false, not {enabled!r}")
    return GatewayConfig(url=normalize_gateway_url(litellm.get("url")), enabled=enabled)


def load_gateway(path: Path) -> GatewayConfig | None:
    """The gateway as the file stands. No file means no gateway."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ClusterConfigError(f"could not read {path}: {exc}") from exc
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ClusterConfigError(f"not valid YAML: {exc}") from exc
    return parse_gateway(payload)


def write_gateway(path: Path, gateway: GatewayConfig | None) -> None:
    """Set the gateway block, or remove it with None, leaving the rest alone.

    NEEDS AN EXISTING CLUSTER FILE. Without one, a deployment reads its nodes
    from SPARK_NODES, and creating the file just to hold `gateway:` would
    make the backend read it as an empty cluster instead. Refused with a
    sentence Settings can show.
    """
    doc = _current_document(path)
    if doc is None or not isinstance(doc.get("nodes"), list):
        raise ClusterConfigError(
            f"client stats are configured in the cluster file ({path.name}), and this "
            "deployment does not have one yet. Nodes still come from SPARK_NODES; "
            "move them into the cluster file first."
        )
    nodes = parse_cluster(path.read_text())

    block = dict(doc.get("gateway") or {})
    if gateway is None:
        block.pop("litellm", None)
    else:
        block["litellm"] = {"url": gateway.url, "enabled": gateway.enabled}

    rest = {k: v for k, v in doc.items() if k != "gateway"}
    # A new block goes first, where the example file puts it; an existing one
    # keeps its place.
    if "gateway" in doc:
        updated = {k: (block if k == "gateway" else v) for k, v in doc.items()}
    else:
        updated = {"gateway": block, **rest}
    if not block:
        updated.pop("gateway", None)

    text = dump_cluster(nodes, preserve=updated)
    parse_gateway(yaml.safe_load(text))
    _atomic_write(path, text)


def _current_document(path: Path) -> Mapping | None:
    """The file's top-level mapping as it stands, or None if there is none to keep."""
    try:
        payload = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return None
    return payload if isinstance(payload, dict) else None


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
