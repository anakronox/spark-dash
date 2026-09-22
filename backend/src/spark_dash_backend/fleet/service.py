"""The controller: collects every Spark on a timer, scores it, serves the
dashboard, and runs updates when a person presses the button.

Everything is files under the data directory:
  fleet.json          inventory
  recipes/            carried copy of NVIDIA's release list
  posture/<n>.json    the latest record per Spark
  facts/<n>.json      the raw facts it was built from
  runs/<id>/          state.json, per-step envelopes, verbatim stdout, logs
"""

from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import ssh, posture as posture_mod
from .executor import Run
from .inventory import Inventory

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("SPARK_FLEET_DATA", "state")).resolve()
PORT = int(os.environ.get("SPARK_FLEET_PORT", "8080"))
INTERVAL_MIN = float(os.environ.get("SPARK_FLEET_INTERVAL_MIN", "60"))
COLLECT_SCRIPT = (HERE / "node_collect.py").read_text()
TLS_MODE = os.environ.get("SPARK_FLEET_TLS", "auto").lower()          # auto | off
TLS_CERT = os.environ.get("SPARK_FLEET_TLS_CERT", "")
TLS_KEY = os.environ.get("SPARK_FLEET_TLS_KEY", "")
ALLOW_PLAIN_PASSWORD = os.environ.get("SPARK_FLEET_ALLOW_PLAIN_PASSWORD", "") == "1"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Service:
    def __init__(self):
        for d in ("posture", "facts", "runs", "recipes"):
            (DATA / d).mkdir(parents=True, exist_ok=True)
        if not any((DATA / "recipes").glob("spark-ota-*.json")):
            for p in (HERE / "recipes").glob("spark-ota-*.json"):
                shutil.copy(p, DATA / "recipes" / p.name)
        self.inv = Inventory(DATA / "fleet.json")
        self.runs: dict[str, Run] = {}
        self._node_locks: dict[str, threading.Lock] = {}
        self.checking: set[str] = set()
        self.last_sweep: str | None = None
        self.next_sweep: str | None = None
        self._load_runs()

    # ── recipes ──────────────────────────────────────────────────────────
    def _carried_recipe_hashes(self) -> dict[str, str]:
        import hashlib
        return {p.name: hashlib.md5(p.read_bytes()).hexdigest() for p in (DATA / "recipes").glob("spark-ota-*.json")}

    def _adopt_recipes(self, files: dict[str, str], from_node: str) -> None:
        for name, text in files.items():
            (DATA / "recipes" / name).write_text(text)
        (DATA / "recipes" / "REFRESHED").write_text(f"{_now()} from {from_node}\n")

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
        (DATA / "facts" / f"{name}.json").write_text(r.out)
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
        rec = posture_mod.build(node, facts, DATA / "recipes")
        rec["recipes_match"] = match
        rec["collect_reason"] = reason
        self._write_posture(name, rec)
        try:
            self.detect_clusters()
        except Exception as e:  # never let topology break a collect
            print("cluster detection failed:", repr(e), file=sys.stderr)
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
        p = DATA / "posture" / f"{name}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, indent=1))
        tmp.replace(p)

    def posture(self, name: str) -> dict | None:
        p = DATA / "posture" / f"{name}.json"
        return json.loads(p.read_text()) if p.exists() else None

    def sweep(self, reason: str = "scheduled") -> None:
        threads = [threading.Thread(target=self.collect_node, args=(n, reason), daemon=True) for n in self.inv.nodes]
        for t in threads: t.start()
        for t in threads: t.join()
        self.last_sweep = _now()

    def scheduler(self) -> None:
        while True:
            try:
                self.sweep()
            except Exception as e:
                print("sweep failed:", e, file=sys.stderr)
            self.next_sweep = datetime.fromtimestamp(time.time() + INTERVAL_MIN * 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            time.sleep(INTERVAL_MIN * 60)

    # ── runs ─────────────────────────────────────────────────────────────
    def _load_runs(self) -> None:
        for d in sorted((DATA / "runs").iterdir()):
            if (d / "state.json").exists():
                st = json.loads((d / "state.json").read_text())
                if st.get("status") == "running":      # the controller died mid-run
                    st["status"] = "failed"; st["message"] = "the controller restarted during this run"
                    st["finished_at"] = st.get("finished_at") or _now()
                    (d / "state.json").write_text(json.dumps(st, indent=1))

    def run_states(self) -> list[dict]:
        out = []
        for d in sorted((DATA / "runs").iterdir(), reverse=True):
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
        run = Run(run_id, DATA / "runs" / run_id, nodes, self, rehearse=rehearse, password=password)
        self.runs[run_id] = run
        run.start()
        return run.state

    def verify_run(self, run_id: str) -> dict:
        """Finish a run that was held at the restart, now that the Spark is back."""
        d = DATA / "runs" / run_id
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
        recipes = posture_mod.load_recipes(DATA / "recipes")
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
                "last_sweep": self.last_sweep, "next_sweep": self.next_sweep, "interval_min": INTERVAL_MIN,
                "ssh_user": ssh.SSH_USER or None, "now": _now()}


SVC: Service


class Handler(BaseHTTPRequestHandler):
    server_version = "spark-fleet-updates"

    def log_message(self, fmt, *args):
        if "/api/fleet" in (args[0] if args else ""):
            return
        super().log_message(fmt, *args)   # request lines only — bodies (which may carry a password) are never logged

    def _secure(self) -> bool:
        """Is this request on a channel a password may travel over?"""
        if isinstance(self.connection, ssl.SSLSocket):
            return True
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            return True
        return ALLOW_PLAIN_PASSWORD

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        path = urlparse(self.path).path
        parts = [p for p in path.split("/") if p]
        try:
            if path in ("/", "/index.html"):
                body = (HERE / "web" / "index.html").read_bytes()
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
            if path == "/api/fleet":
                return self._json(200, SVC.fleet())
            if parts[:2] == ["api", "nodes"] and len(parts) == 4 and parts[3] == "updates":
                p = SVC.posture(parts[2])
                return self._json(200 if p else 404, p or {"error": "unknown node"})
            if path == "/api/runs":
                return self._json(200, SVC.run_states())
            if parts[:2] == ["api", "runs"] and len(parts) == 3:
                d = DATA / "runs" / parts[2]
                return self._json(200, json.loads((d / "state.json").read_text())) if (d / "state.json").exists() else self._json(404, {"error": "no such run"})
            if parts[:2] == ["api", "runs"] and len(parts) == 5 and parts[4] == "log":
                f = DATA / "runs" / parts[2] / f"{parts[3]}.log"
                text = f.read_text() if f.exists() else ""
                return self._json(200, {"lines": text.splitlines()[-400:]})
            self._json(404, {"error": "not found"})
        except Exception as e:
            self._json(500, {"error": repr(e)})

    def do_POST(self):
        path = urlparse(self.path).path
        parts = [p for p in path.split("/") if p]
        try:
            body = self._body()
            if path == "/api/nodes":
                node = SVC.inv.add(body.get("name", ""), body.get("host", ""), body.get("user"))
                threading.Thread(target=SVC.collect_node, args=(node, "added"), daemon=True).start()
                return self._json(201, node)
            if path == "/api/test":
                r = ssh.reachable(body.get("host", ""), user=body.get("user"))
                vendor = r.out.strip().splitlines()[-1] if r.ok and len(r.out.strip().splitlines()) > 1 else ""
                return self._json(200, {"ok": r.ok, "board": vendor, "error": (r.err or "").strip()[:200]})
            if path == "/api/check":
                threading.Thread(target=SVC.sweep, args=("manual",), daemon=True).start()
                return self._json(202, {"ok": True})
            if parts[:2] == ["api", "nodes"] and len(parts) == 4:
                name, action = parts[2], parts[3]
                node = SVC.inv.get(name)
                if not node:
                    return self._json(404, {"error": "unknown node"})
                if action == "remove":
                    SVC.inv.remove(name); return self._json(200, {"ok": True})
                if action == "rename":
                    SVC.inv.rename(name, body.get("name", ""))
                    old = DATA / "posture" / f"{name}.json"
                    if old.exists(): old.rename(DATA / "posture" / f"{body['name'].strip()}.json")
                    return self._json(200, {"ok": True})
                if action == "check":
                    threading.Thread(target=SVC.collect_node, args=(node, "manual"), daemon=True).start()
                    return self._json(202, {"ok": True})
                if action in ("update", "rehearse"):
                    password = body.get("password") or None
                    if password and not self._secure():
                        return self._json(400, {"error": "a password is only accepted over HTTPS — open this page at https://, "
                                                          "or put the controller behind a TLS proxy"})
                    return self._json(202, SVC.start_update(name, rehearse=(action == "rehearse"), password=password))
            if parts[:2] == ["api", "runs"] and len(parts) == 4 and parts[3] == "verify":
                return self._json(202, SVC.verify_run(parts[2]))
            if parts[:2] == ["api", "runs"] and len(parts) == 4 and parts[3] == "stop":
                run = SVC.runs.get(parts[2])
                if not run:
                    return self._json(404, {"error": "no such active run"})
                run.stop_requested = True
                return self._json(200, {"ok": True})
            if path == "/api/clusters/rename":
                SVC.inv.name_cluster(body.get("members", []), body.get("name", "")); return self._json(200, {"ok": True})
            if path == "/api/units":
                SVC.inv.set_units(body.get("units", [])); return self._json(200, {"ok": True})
            self._json(404, {"error": "not found"})
        except (ValueError, KeyError) as e:
            self._json(400, {"error": str(e)})
        except Exception as e:
            self._json(500, {"error": repr(e)})


def _tls_context() -> ssl.SSLContext | None:
    """HTTPS by default. Uses the certificate you give it, else makes a
    self-signed one under the data directory on first start and keeps it."""
    if TLS_MODE == "off":
        return None
    cert, key = Path(TLS_CERT) if TLS_CERT else DATA / "tls" / "cert.pem", Path(TLS_KEY) if TLS_KEY else DATA / "tls" / "key.pem"
    if not (cert.exists() and key.exists()):
        if TLS_CERT or TLS_KEY:
            raise SystemExit(f"TLS certificate or key not found: {cert} / {key}")
        cert.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                        "-nodes", "-days", "3650", "-subj", "/CN=spark-fleet-updates",
                        "-addext", "subjectAltName=DNS:localhost,DNS:spark-fleet-updates,IP:127.0.0.1",
                        "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
        os.chmod(key, 0o600)
        print(f"made a self-signed certificate at {cert} — your browser will ask you to trust it once", flush=True)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert), str(key))
    return ctx


def main() -> None:
    global SVC
    SVC = Service()
    threading.Thread(target=SVC.scheduler, daemon=True, name="scheduler").start()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    ctx = _tls_context()
    if ctx:
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    scheme = "https" if ctx else "http"
    print(f"spark-fleet-updates on {scheme}://0.0.0.0:{PORT} · data {DATA} · {len(SVC.inv.nodes)} Sparks · checks every {INTERVAL_MIN:g} min"
          + ("" if ctx or ALLOW_PLAIN_PASSWORD else " · plain HTTP: passwords refused unless behind a TLS proxy"), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
