"""The controller: collects every Spark on a timer, scores it, and runs
updates when a person presses the button.

It no longer serves anything. Upstream this module also held a stdlib HTTP
server, a self-signed TLS context and a `main()`; AL3b took those out, because
the dashboard's backend is the server and `FleetUpdates.svelte` is the page.
What is left is a plain object the backend owns: give it a data directory and
a check interval, start its scheduler in the app's lifespan, stop it on
shutdown. Nothing here reads the environment, so a test can build one on a
tmp_path and a second instance cannot fight the first over module globals.

ONE INSTANCE PER PROCESS, and one process: `self.runs`, the per-node locks and
`checking` are in memory. See AL4.4 -- uvicorn runs no workers here, and two
would mean two schedulers sweeping the same Sparks.

Everything is files under the data directory:
  fleet.json          inventory
  recipes/            carried copy of NVIDIA's release list
  posture/<n>.json    the latest record per Spark
  facts/<n>.json      the raw facts it was built from
  runs/<id>/          state.json, per-step envelopes, verbatim stdout, logs
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import ssh, posture as posture_mod
from .executor import Run
from .inventory import Inventory

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
# Read as text and piped to each node's python3 -- never imported. That is why
# it has to be inside the package rather than beside it: AL3a checked it ships.
COLLECT_SCRIPT = (HERE / "node_collect.py").read_text()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Service:
    def __init__(self, data_dir: Path, interval_min: float = 60.0):
        self.data = Path(data_dir).resolve()
        self.interval_min = interval_min
        for d in ("posture", "facts", "runs", "recipes"):
            (self.data / d).mkdir(parents=True, exist_ok=True)
        if not any((self.data / "recipes").glob("spark-ota-*.json")):
            for p in (HERE / "recipes").glob("spark-ota-*.json"):
                shutil.copy(p, self.data / "recipes" / p.name)
        self.inv = Inventory(self.data / "fleet.json")
        self.runs: dict[str, Run] = {}
        self._node_locks: dict[str, threading.Lock] = {}
        self.checking: set[str] = set()
        self.last_sweep: str | None = None
        self.next_sweep: str | None = None
        # The scheduler waits on this rather than sleeping, so shutdown is
        # immediate instead of up to an hour late.
        self._stop = threading.Event()
        self._scheduler: threading.Thread | None = None
        self._load_runs()

    # ── recipes ──────────────────────────────────────────────────────────
    def _carried_recipe_hashes(self) -> dict[str, str]:
        import hashlib
        return {p.name: hashlib.md5(p.read_bytes()).hexdigest() for p in (self.data / "recipes").glob("spark-ota-*.json")}

    def _adopt_recipes(self, files: dict[str, str], from_node: str) -> None:
        for name, text in files.items():
            (self.data / "recipes" / name).write_text(text)
        (self.data / "recipes" / "REFRESHED").write_text(f"{_now()} from {from_node}\n")

    # ── collecting ───────────────────────────────────────────────────────
    def collect_node(self, node: dict, reason: str = "scheduled") -> dict | None:
        name = node["name"]
        lock = self._node_locks.setdefault(name, threading.Lock())
        with lock:
            self.checking.add(name)
            try:
                return self._collect_locked(node, reason)
            finally:
                self.checking.discard(name)

    def _collect_locked(self, node: dict, reason: str) -> dict | None:
        name, host, user = node["name"], node["host"], node.get("user")
        r = ssh.run_python(host, "OPTIONS = {}\n" + COLLECT_SCRIPT, user=user, timeout=240)
        if not r.ok or not r.out.strip():
            rec = {"name": name, "host": host, "reachable": False, "collected_at": _now(),
                   "error": (r.err or f"exit {r.rc}").strip()[:300]}
            prev = self.posture(name)
            if prev and prev.get("reachable"):
                rec["last_good"] = prev
            self._write_posture(name, rec)
            return rec
        facts = json.loads(r.out)
        (self.data / "facts" / f"{name}.json").write_text(r.out)
        carried = self._carried_recipe_hashes()
        match = facts.get("recipes") == carried
        if not match and facts.get("recipes"):
            # a node with a newer release list than ours: adopt it, once, from a node that has more
            theirs = facts["recipes"]
            if set(theirs) >= set(carried):
                r2 = ssh.run_python(host, "OPTIONS = {'recipes': True}\n" + COLLECT_SCRIPT, user=user, timeout=240)
                if r2.ok:
                    try:
                        self._adopt_recipes(json.loads(r2.out).get("recipe_files", {}), name)
                        match = True
                    except json.JSONDecodeError:
                        pass
        rec = posture_mod.build(node, facts, self.data / "recipes")
        rec["recipes_match"] = match
        rec["collect_reason"] = reason
        self._write_posture(name, rec)
        try:
            self.detect_clusters()
        except Exception:  # never let topology break a collect
            log.exception("cluster detection failed")
        return rec

    # ── who is cabled to whom ────────────────────────────────────────────
    def detect_clusters(self) -> list[dict]:
        """Connected components over direct ConnectX-7 links between Sparks.
        A link is an LLDP neighbor on a CX7 port that resolves to another Spark
        in the inventory (by port MAC, chassis MAC or hostname); the ARP table
        on the same ports is the fallback. Switches never join a cluster."""
        nodes = self.inv.nodes
        post = {n["name"]: self.posture(n["name"]) for n in nodes}
        post = {k: v for k, v in post.items() if v and v.get("reachable") and v.get("fabric")}
        macs = {k: set(v["fabric"].get("macs", [])) for k, v in post.items()}
        hosts = {k: (v.get("hostname") or "").lower() for k, v in post.items()}
        speeds = {(k, p_["ifname"]): p_.get("speed_mbps", 0) for k, v in post.items() for p_ in v["fabric"].get("ports", [])}

        def resolve(a, chassis_name="", chassis_id="", port_id="", lladdr=""):
            for b in post:
                if b == a:
                    continue
                if port_id and port_id.lower() in macs[b]: return b
                if lladdr and lladdr.lower() in macs[b]: return b
                if chassis_id and chassis_id.lower() in macs[b]: return b
                if chassis_name and chassis_name.lower() == hosts[b]: return b
            return None

        # One entry per local CX7 port. The ARP table names the port the traffic
        # actually goes to, so it wins; LLDP floods across joined ports and is the
        # fallback when a port has no ARP neighbour yet.
        port_links: dict[tuple, dict] = {}
        port_by_mac = {p_["mac"].lower(): (k, p_["ifname"]) for k, v in post.items() for p_ in v["fabric"].get("ports", [])}
        for a, v in post.items():
            for e in v["fabric"].get("neigh", []):
                hit = port_by_mac.get((e.get("lladdr") or "").lower())
                if hit and hit[0] != a and (a, e["ifname"]) not in port_links:
                    port_links[(a, e["ifname"])] = {"b": hit[0], "b_port": hit[1], "via": "arp"}
            for l in v["fabric"].get("lldp", []):
                if (a, l["ifname"]) in port_links:
                    continue
                b = resolve(a, l.get("chassis_name", ""), l.get("chassis_id", ""), l.get("port_id", ""))
                if b:
                    bp = port_by_mac.get((l.get("port_id") or "").lower(), (None, l.get("port_descr") or ""))[1]
                    port_links[(a, l["ifname"])] = {"b": b, "b_port": bp, "via": "lldp"}
        links: dict[frozenset, dict] = {}
        for (a, ap), d in port_links.items():
            b, bp = d["b"], d["b_port"]
            key = tuple(sorted([(a, ap), (b, bp)]))          # symmetric: the same cable seen from both ends
            links.setdefault(frozenset([a, b]), {})[key] = {"a": key[0][0], "a_port": key[0][1], "b": key[1][0], "b_port": key[1][1],
                                                           "speed_mbps": max(speeds.get((a, ap), 0), speeds.get((b, bp), 0)), "via": d["via"]}
        parent = {n["name"]: n["name"] for n in nodes}
        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]; x = parent[x]
            return x
        for pair in links:
            a, b = tuple(pair)
            parent[find(a)] = find(b)
        groups: dict[str, list[str]] = {}
        for n in parent:
            groups.setdefault(find(n), []).append(n)
        clusters = []
        for members in groups.values():
            if len(members) < 2:
                continue
            members = sorted(members)
            ls = [l for pair, d in links.items() if pair <= set(members) for l in d.values()]
            # one line per physical link: a_port on a to b_port on b
            seen, uniq = set(), []
            for l in sorted(ls, key=lambda l: (l["a"], l["a_port"], l["b"], l["b_port"])):
                k = (l["a"], l["a_port"], l["b"], l["b_port"])
                if k in seen or (not l["a_port"] and not l["b_port"]):
                    continue
                seen.add(k); uniq.append(l)
            clusters.append({"members": members, "links": uniq})
        clusters.sort(key=lambda c: c["members"])
        self.inv.set_clusters(clusters)
        return clusters

    def _write_posture(self, name: str, rec: dict) -> None:
        p = self.data / "posture" / f"{name}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, indent=1))
        tmp.replace(p)

    def posture(self, name: str) -> dict | None:
        p = self.data / "posture" / f"{name}.json"
        return json.loads(p.read_text()) if p.exists() else None

    def sweep(self, reason: str = "scheduled") -> None:
        threads = [threading.Thread(target=self.collect_node, args=(n, reason), daemon=True) for n in self.inv.nodes]
        for t in threads: t.start()
        for t in threads: t.join()
        self.last_sweep = _now()

    def start(self) -> None:
        """Begin the hourly check. Called from the app's lifespan; idempotent."""
        if self._scheduler and self._scheduler.is_alive():
            return
        self._stop.clear()
        self._scheduler = threading.Thread(target=self.scheduler, daemon=True, name="fleet-scheduler")
        self._scheduler.start()
        log.info("fleet updater started: %d Spark(s), checking every %g min",
                 len(self.inv.nodes), self.interval_min)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop checking. A run already under way is NOT cancelled -- it is a
        systemd unit on the Spark and outlives this process by design."""
        self._stop.set()
        if self._scheduler:
            self._scheduler.join(timeout=timeout)

    def scheduler(self) -> None:
        while not self._stop.is_set():
            try:
                self.sweep()
            except Exception:
                log.exception("fleet sweep failed")
            self.next_sweep = datetime.fromtimestamp(time.time() + self.interval_min * 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._stop.wait(self.interval_min * 60)

    # ── runs ─────────────────────────────────────────────────────────────
    def _load_runs(self) -> None:
        for d in sorted((self.data / "runs").iterdir()):
            if (d / "state.json").exists():
                st = json.loads((d / "state.json").read_text())
                if st.get("status") == "running":      # the controller died mid-run
                    st["status"] = "failed"; st["message"] = "the controller restarted during this run"
                    st["finished_at"] = st.get("finished_at") or _now()
                    (d / "state.json").write_text(json.dumps(st, indent=1))

    def run_states(self) -> list[dict]:
        out = []
        for d in sorted((self.data / "runs").iterdir(), reverse=True):
            if (d / "state.json").exists():
                out.append(json.loads((d / "state.json").read_text()))
        return out

    def active_run_for(self, name: str) -> dict | None:
        for st in self.run_states():
            if st["status"] == "running" and any(n["name"] == name for n in st["nodes"]):
                return st
        return None

    def start_update(self, name: str, rehearse: bool = False, password: str | None = None) -> dict:
        if not self.inv.get(name):
            raise KeyError(name)
        members = self.inv.unit_of(name)
        for m in members:
            if self.active_run_for(m):
                raise ValueError(f"{m} is already being updated")
        # the member that was asked for goes first
        members.sort(key=lambda m: m != name)
        nodes = [self.inv.get(m) for m in members]
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + name + ("-rehearsal" if rehearse else "")
        if rehearse:
            nodes = [self.inv.get(name)]          # a rehearsal is one node, whatever it is cabled to
        run = Run(run_id, self.data / "runs" / run_id, nodes, self, rehearse=rehearse, password=password)
        self.runs[run_id] = run
        run.start()
        return run.state

    def verify_run(self, run_id: str) -> dict:
        """Finish a run that was held at the restart, now that the Spark is back."""
        d = self.data / "runs" / run_id
        if not (d / "state.json").exists():
            raise KeyError(run_id)
        st = json.loads((d / "state.json").read_text())
        names = [x["name"] for x in st["nodes"]]
        nodes = [self.inv.get(nm) for nm in names if self.inv.get(nm)]
        run = self.runs.get(run_id) or Run(run_id, d, nodes, self, rehearse=st.get("rehearsal", False))
        run.state = st
        self.runs[run_id] = run
        def go():
            for node in nodes:
                x = run._node(node["name"])
                if x["status"] == "ok":
                    continue
                run.verify_node(node)
            run.finish_late()
        threading.Thread(target=go, daemon=True).start()
        return st

    # ── the fleet view ───────────────────────────────────────────────────
    def fleet(self) -> dict:
        nodes = []
        recipes = posture_mod.load_recipes(self.data / "recipes")
        latest = max((r for r in recipes if not r.is_ebeta), key=lambda r: r.release_date)
        for n in self.inv.nodes:
            p = self.posture(n["name"]) or {"name": n["name"], "host": n["host"], "reachable": None}
            p["checking"] = n["name"] in self.checking
            p["pair"] = [m for m in self.inv.unit_of(n["name"]) if m != n["name"]]
            p["run"] = self.active_run_for(n["name"])
            last = next((st for st in self.run_states() if any(x["name"] == n["name"] for x in st["nodes"])), None)
            p["last_run"] = last
            nodes.append(p)
        return {"nodes": nodes, "clusters": self.inv.data.get("clusters", []),
                "latest": {"name": latest.name, "external_name": latest.external_name,
                           "date": latest.release_date_str[:10]},
                "last_sweep": self.last_sweep, "next_sweep": self.next_sweep, "interval_min": self.interval_min,
                "ssh_user": ssh.SSH_USER or None, "now": _now()}
