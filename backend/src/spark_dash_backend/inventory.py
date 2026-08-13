"""Which nodes exist, read from the file Prometheus already uses.

Deliberately shares Prometheus's `file_sd` inventory rather than keeping a
second list. Two lists would drift, and the failure would be quiet and
confusing: a node visible in history but absent from the live view, or the
reverse. One file means adding a node is one edit, which is the whole Phase 2
scaling story.

Re-read on a TTL so adding a node doesn't need a backend restart — matching
Prometheus's own `refresh_interval` behavior.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

DEFAULT_AGENT_PORT = 9500


@dataclass(frozen=True)
class Node:
    """One GX10, as both a scrape target and a live-poll target."""

    node_id: str
    address: str  # host:port, as Prometheus sees it

    @property
    def agent_url(self) -> str:
        return f"http://{self.address}"

    @property
    def snapshot_url(self) -> str:
        return f"{self.agent_url}/snapshot"


def parse_file_sd(content: str) -> list[Node]:
    """Parse Prometheus `file_sd` YAML into nodes.

    Expected shape:

        - targets: ['192.168.50.61:9500']
          labels:
            node: gx10-1

    A target without a `node` label falls back to its host, so a
    half-configured inventory still yields something usable rather than
    silently dropping the node. Malformed entries are skipped with a warning
    rather than raising — one bad line shouldn't blind the whole dashboard.
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
            address = target.strip()
            node_id = str(labels.get("node") or address.split(":")[0])

            # A duplicate node id would double-count in cluster aggregates.
            if node_id in seen:
                log.warning("duplicate node id %r in inventory; ignoring %s", node_id, address)
                continue
            seen.add(node_id)
            nodes.append(Node(node_id=node_id, address=address))

    return nodes


class Inventory:
    """Caches the parsed inventory, re-reading when the TTL expires."""

    def __init__(self, path: Path, ttl_s: float = 30.0) -> None:
        self._path = path
        self._ttl_s = ttl_s
        self._nodes: list[Node] = []
        self._loaded_at = 0.0

    def nodes(self, now: float | None = None) -> list[Node]:
        now = time.monotonic() if now is None else now
        if not self._nodes or (now - self._loaded_at) >= self._ttl_s:
            self._nodes = self._load()
            self._loaded_at = now
        return self._nodes

    def _load(self) -> list[Node]:
        try:
            content = self._path.read_text()
        except OSError:
            # Keep serving the previous inventory rather than dropping every
            # node because the file was briefly unreadable mid-edit.
            log.warning("could not read inventory at %s", self._path)
            return self._nodes

        nodes = parse_file_sd(content)
        if not nodes:
            log.warning("inventory at %s yielded no nodes", self._path)
        return nodes
