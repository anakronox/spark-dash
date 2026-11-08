"""API keys for llama.cpp routers and engines, and the token agents present
to fetch them.

WHY. A llama.cpp router started with `--api-key` / `--api-key-file` answers
401 on everything but `/health`, `/v1/models` and `/metrics` included
(measured on sparky's lab router, 2026-11-08). The agent polls exactly those,
so a keyed router reads as unreachable unless the agent sends its key.
A keyed TensorFold answers `/metrics` without one but `/v1/models` only with
it (sparketa, 2026-11-08), and that listing is where the agent gets the model's
name; vLLM keys `/v1` only, and SGLang may key `/metrics` too. So an engine's
key goes on both of the agent's requests.

WHERE THE KEYS LIVE. Set in Settings, per router, and kept centrally in a file
beside the cluster file, 0600, as the channel secrets are: `cluster.yml` is
served to the browser, so a key must never be in it, and the browser only ever
learns whether one is set and its last four characters.

SEALED (CU6). An agent with AGENT_SEALED_KEYS asks without the token and gets
its keys sealed with it (`spark_dash_common.sealed`), so neither the token nor
a key crosses the LAN in clear. Opt-in: with "require encrypted keys" on in
Settings (`agent-keys.sealed` beside the token), the plain answer below is
never given, token or not. Sealed keys are given whether or not it is on, so
nodes can be switched before it is.

HOW AN AGENT GETS ONE. With its config, from `/api/agent-config`, but only when
the request carries the agents' shared token. One token for every node: the
node stack stays byte-identical everywhere (one repo, one agent config), so
this is a shared secret, not per-agent identity. It lives centrally in
`agent.token` beside the cluster file (made in Settings, shown once), and on
each node in the stack's `AGENT_TOKEN`. Without the token, or with no token
configured centrally, an agent gets its config exactly as before, minus keys.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlparse

from .channels import hint
from .cluster import ClusterConfigError, ClusterNode

SECRETS_FILE = "router-keys.secrets.json"
TOKEN_FILE = "agent.token"
SEALED_FILE = "agent-keys.sealed"


def key_id(node_id: str, port: int) -> str:
    """How a router or engine is named in the secrets file: its node and its
    port, which is also how Settings names it (runtimes are written as ports).
    A port is one server on a node, whichever runtime it is."""
    return f"{node_id}:{int(port)}"


def port_of(url: str) -> int | None:
    try:
        return urlparse(url).port
    except ValueError:
        return None


# --- the keys --------------------------------------------------------------------------------

def _secrets_path(cluster_path: Path) -> Path:
    return cluster_path.parent / SECRETS_FILE


def read_keys(cluster_path: Path) -> dict[str, str]:
    try:
        data = json.loads(_secrets_path(cluster_path).read_text())
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_owner_only(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def write_keys(cluster_path: Path, values: dict[str, str]) -> None:
    _write_owner_only(_secrets_path(cluster_path), json.dumps(values))


def check_key(value: str) -> str:
    """A key as llama.cpp compares it: one token, no whitespace (the router
    trims its key file, the agent would send the spaces). No minimum length:
    the router's operator chose the key, and it is whatever it is."""
    v = value.strip()
    if not v:
        raise ClusterConfigError("the key is empty")
    if len(v) > 512:
        raise ClusterConfigError("an API key is at most 512 characters")
    if not v.isascii() or not v.isprintable() or any(c.isspace() for c in v):
        raise ClusterConfigError("an API key is printable ASCII without spaces")
    return v


def endpoints(node: ClusterNode) -> list[str]:
    """Every URL the node's agent polls that can take a key: its llama.cpp
    routers and its engines' /metrics endpoints."""
    urls = [r.url for r in node.runtimes.llama_routers]
    for engine_urls in node.runtimes.engines.values():
        urls.extend(engine_urls)
    return urls


def find_endpoint(nodes: list[ClusterNode], node_id: str, port: int) -> ClusterNode | None:
    """The node, if it has a router or an engine on that port."""
    node = next((n for n in nodes if n.node_id == node_id), None)
    if node is None:
        return None
    if any(port_of(u) == port for u in endpoints(node)):
        return node
    return None


def status(cluster_path: Path) -> dict[str, dict]:
    """What Settings may see: per router or engine, whether a key is set and
    its end."""
    return {k: {"set": True, "hint": hint(v)} for k, v in read_keys(cluster_path).items()}


def prune(cluster_path: Path, nodes: list[ClusterNode]) -> None:
    """Drop the keys of routers and engines no longer in the cluster file, so
    one removed in Settings does not leave its key behind."""
    stored = read_keys(cluster_path)
    if not stored:
        return
    live = {
        key_id(n.node_id, p)
        for n in nodes
        for u in endpoints(n)
        if (p := port_of(u)) is not None
    }
    keep = {k: v for k, v in stored.items() if k in live}
    if keep != stored:
        write_keys(cluster_path, keep)


def keys_for(cluster_path: Path, node: ClusterNode) -> dict[str, str]:
    """This node's keys by the URL the agent polls: a router's URL, an
    engine's /metrics URL."""
    stored = read_keys(cluster_path)
    out = {}
    for url in endpoints(node):
        port = port_of(url)
        key = stored.get(key_id(node.node_id, port)) if port is not None else None
        if key:
            out[url] = key
    return out


# --- the agents' shared token -----------------------------------------------------------------

def token_path(cluster_path: Path) -> Path:
    return cluster_path.parent / TOKEN_FILE


def read_token(cluster_path: Path) -> str | None:
    try:
        return token_path(cluster_path).read_text().strip() or None
    except OSError:
        return None


def new_token(cluster_path: Path) -> str:
    """A fresh token, owner-only, written atomically. Returned once, for
    Settings to show; afterwards only its last four are. The old one stops
    working at once, so every node's AGENT_TOKEN must be updated with it."""
    token = "sda-" + secrets.token_urlsafe(32)
    _write_owner_only(token_path(cluster_path), token + "\n")
    return token


def remove_token(cluster_path: Path) -> None:
    token_path(cluster_path).unlink(missing_ok=True)


def presented_token(authorization: str | None) -> str:
    given = authorization or ""
    return given[7:].strip() if given[:7].lower() == "bearer " else ""


def token_matches(cluster_path: Path, authorization: str | None) -> bool:
    """Constant-time. False when no token is configured: keys are then never
    sent, whatever the request carries."""
    expected = read_token(cluster_path)
    presented = presented_token(authorization)
    if not expected or not presented:
        return False
    return secrets.compare_digest(presented.encode(), expected.encode())


# --- sealed keys required --------------------------------------------------------------------

def sealed_required(cluster_path: Path) -> bool:
    """On, the backend sends keys only sealed: an agent presenting the token
    as a bearer gets none."""
    return (cluster_path.parent / SEALED_FILE).exists()


def set_sealed_required(cluster_path: Path, on: bool) -> None:
    flag = cluster_path.parent / SEALED_FILE
    if on:
        _write_owner_only(flag, "keys are sent to agents sealed only\n")
    else:
        flag.unlink(missing_ok=True)
