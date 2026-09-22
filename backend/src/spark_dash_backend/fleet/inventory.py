"""The fleet: which Sparks, and which of them work as a pair.

A JSON file, edited through the GUI. `units` are groups that must end an
update on the same release — the memory-pooling pair — so updating one member
updates all of them."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Inventory:
    def __init__(self, path: Path):
        self.path = path
        self.data = {"nodes": [], "units": []}
        if path.exists():
            self.data = json.loads(path.read_text())
        self.data.setdefault("nodes", [])
        self.data.setdefault("units", [])
        self.data.setdefault("clusters", [])        # detected from the fabric, rewritten each sweep
        self.data.setdefault("cluster_names", {})   # "a+b" -> the name a person gave it

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)

    @property
    def nodes(self) -> list[dict]:
        return list(self.data["nodes"])

    def get(self, name: str) -> dict | None:
        return next((n for n in self.data["nodes"] if n["name"] == name), None)

    def add(self, name: str, host: str, user: str | None = None) -> dict:
        name = name.strip()
        host = host.strip()
        if not name or not host:
            raise ValueError("name and host are required")
        if any(c in name for c in "/\\ \t"):
            raise ValueError("name may not contain spaces or slashes")
        with _lock:
            if self.get(name):
                raise ValueError(f"{name} already exists")
            node = {"name": name, "host": host, "added_at": _now()}
            if user:
                node["user"] = user
            self.data["nodes"].append(node)
            self._save()
        return node

    def remove(self, name: str) -> None:
        with _lock:
            before = len(self.data["nodes"])
            self.data["nodes"] = [n for n in self.data["nodes"] if n["name"] != name]
            if len(self.data["nodes"]) == before:
                raise KeyError(name)
            self.data["units"] = [[m for m in u if m != name] for u in self.data["units"]]
            self.data["units"] = [u for u in self.data["units"] if len(u) > 1]
            self.data["clusters"] = [c for c in self.data["clusters"] if name not in c["members"]]
            self._save()

    def rename(self, name: str, new_name: str) -> None:
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("a name is required")
        with _lock:
            node = self.get(name)
            if not node:
                raise KeyError(name)
            if self.get(new_name):
                raise ValueError(f"{new_name} already exists")
            node["name"] = new_name
            self.data["units"] = [[new_name if m == name else m for m in u] for u in self.data["units"]]
            for c in self.data["clusters"]:
                c["members"] = sorted(new_name if m == name else m for m in c["members"])
            self.data["cluster_names"] = {self.cluster_key([new_name if m == name else m for m in k.split("+")]): v
                                          for k, v in self.data["cluster_names"].items()}
            self._save()

    def set_units(self, units: list[list[str]]) -> None:
        with _lock:
            self.data["units"] = [list(u) for u in units if len(u) > 1]
            self._save()

    def unit_of(self, name: str) -> list[str]:
        """Every node that must be updated together with `name`, itself included:
        the detected cluster it is cabled into, else a manually declared unit."""
        for c in self.data["clusters"]:
            if name in c["members"]:
                return list(c["members"])
        for u in self.data["units"]:
            if name in u:
                return list(u)
        return [name]

    @staticmethod
    def cluster_key(members: list[str]) -> str:
        return "+".join(sorted(members))

    def set_clusters(self, clusters: list[dict]) -> None:
        with _lock:
            for c in clusters:
                c["name"] = self.data["cluster_names"].get(self.cluster_key(c["members"])) or " + ".join(sorted(c["members"]))
            if clusters != self.data["clusters"]:
                self.data["clusters"] = clusters
                self._save()

    def name_cluster(self, members: list[str], name: str) -> None:
        with _lock:
            key = self.cluster_key(members)
            if name.strip():
                self.data["cluster_names"][key] = name.strip()
            else:
                self.data["cluster_names"].pop(key, None)
            for c in self.data["clusters"]:
                if self.cluster_key(c["members"]) == key:
                    c["name"] = name.strip() or " + ".join(sorted(c["members"]))
            self._save()
