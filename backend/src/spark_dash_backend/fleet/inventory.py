"""Which Sparks the fleet updater checks — and nothing else about them (AL3d).

**This file used to be a second inventory.** It held a name, a host and a login
per Spark, beside the name, host and cluster `cluster.yml` already held, and
AK7's Settings checkbox existed to copy one into the other. Two lists of the
same machines drift: a host edited in `cluster.yml` left the fleet dialling the
old address, which is why the panel had a "host differs" state at all.

So it holds **enrolment only**: a set of node ids, and the answer to "is this
feature switched on". Everything else — the address to dial, who is cabled to
whom, what a Spark is called — is resolved through `resolve`, which the backend
wires to its own inventory. A node cannot differ from itself.

Two consequences worth stating, because they replace features rather than
dropping them:

* **Renaming is not an operation here.** A dashboard node's id *is* the fleet
  name, so a rename in `cluster.yml` is a remove and an add, and the old
  `rename` route (never exposed by the dashboard anyway) is gone.
* **Units are not declared, they are read.** `unit_of` returns the other
  enrolled members of the same `cluster:` in `cluster.yml`, because that is
  already the thing that means "these pool memory and must end on the same
  release". The LLDP detection survives as a *report* — see
  `Service.fabric_disagreements` — since what is cabled and what is configured
  disagreeing is worth saying out loud, and was previously invisible.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

_lock = threading.Lock()

#: Given a node id, everything the fleet needs to reach and group it, or None
#: when the dashboard has never heard of it.
Resolver = Callable[[str], dict | None]


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Inventory:
    def __init__(self, path: Path, resolve: Resolver | None = None):
        self.path = path
        self.resolve: Resolver = resolve or (lambda name: None)
        self.data = {"enrolled": [], "clusters": [], "enabled": True}
        if path.exists():
            self.data = json.loads(path.read_text())
        self.data.setdefault("clusters", [])       # detected from the fabric, rewritten each sweep
        self.data.setdefault("enabled", True)      # AL5's second knob: the feature is on, or quiet
        self._migrate()

    def _migrate(self) -> None:
        """A fleet.json written before AL3d carries the whole node records.

        Keep the enrolment and drop the rest: the hosts in it are the ones that
        could go stale, and the live deployment's list is not worth making
        somebody retype. `units` and `cluster_names` go — pairing comes from
        `cluster.yml` now, and a name from the dashboard's own cluster name.
        """
        if "enrolled" in self.data:
            self.data.setdefault("enrolled", [])
            return
        self.data["enrolled"] = [n["name"] for n in self.data.pop("nodes", []) if n.get("name")]
        self.data.pop("units", None)
        self.data.pop("cluster_names", None)
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)

    # ── the switch (AL5) ─────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return bool(self.data.get("enabled", True))

    def set_enabled(self, on: bool) -> None:
        with _lock:
            self.data["enabled"] = bool(on)
            self._save()

    # ── enrolment ────────────────────────────────────────────────────────
    @property
    def enrolled(self) -> list[str]:
        return list(self.data["enrolled"])

    @property
    def nodes(self) -> list[dict]:
        """Every enrolled Spark the dashboard can still place."""
        return [n for n in (self.resolve(x) for x in self.data["enrolled"]) if n]

    @property
    def orphans(self) -> list[str]:
        """Enrolled, but `cluster.yml` no longer knows the name.

        Not silently dropped: the fleet has run records and posture for it, and
        somebody should decide whether that was a rename or a removal.
        """
        return [x for x in self.data["enrolled"] if not self.resolve(x)]

    def get(self, name: str) -> dict | None:
        return self.resolve(name) if name in self.data["enrolled"] else None

    def add(self, name: str, host: str | None = None, user: str | None = None) -> dict:
        """Enrol a node the dashboard already knows.

        `host` and `user` are accepted and ignored: the address comes from
        `cluster.yml` and the login from the backend's settings. They stay in
        the signature because the panel and the tests still pass them, and
        because a caller that thinks it is choosing an address should get the
        real one rather than an error.
        """
        name = name.strip()
        if not name:
            raise ValueError("a name is required")
        node = self.resolve(name)
        if not node:
            raise ValueError(f"{name} is not a node this dashboard knows")
        with _lock:
            if name not in self.data["enrolled"]:
                self.data["enrolled"].append(name)
                self.data.setdefault("enrolled_at", {})[name] = _now()
                self._save()
        return node

    def remove(self, name: str) -> None:
        with _lock:
            if name not in self.data["enrolled"]:
                raise KeyError(name)
            self.data["enrolled"] = [x for x in self.data["enrolled"] if x != name]
            self.data.get("enrolled_at", {}).pop(name, None)
            self.data["clusters"] = [c for c in self.data["clusters"] if name not in c["members"]]
            self._save()

    # ── who is updated together ──────────────────────────────────────────
    def unit_of(self, name: str) -> list[str]:
        """Every enrolled Spark that must end an update on the same release as
        `name`, itself included.

        `cluster.yml`'s `cluster:` is the source, because it is already the
        thing that says these pool memory: a model spans them, so a release
        that lands on one and not the other is a broken pair, not a partial
        rollout. A standalone Spark is a unit of one.
        """
        me = self.resolve(name)
        if not me or not me.get("cluster"):
            return [name]
        return sorted(
            {name} | {n["name"] for n in self.nodes if n.get("cluster") == me["cluster"]}
        )

    # ── the fabric, as a report ──────────────────────────────────────────
    @staticmethod
    def cluster_key(members: list[str]) -> str:
        return "+".join(sorted(members))

    def set_clusters(self, clusters: list[dict]) -> None:
        """What the ConnectX-7 cabling says. Display and cross-check only — it
        no longer decides what gets updated together."""
        with _lock:
            for c in clusters:
                members = sorted(c["members"])
                named = {n["name"]: n.get("cluster") for n in self.nodes}
                config = {named.get(m) for m in members if named.get(m)}
                c["name"] = config.pop() if len(config) == 1 else " + ".join(members)
            if clusters != self.data["clusters"]:
                self.data["clusters"] = clusters
                self._save()
