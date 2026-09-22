"""Apply an update to one Spark, or to every member of a pair in turn.

The sequence is NVIDIA's own (apt full-upgrade, fwupdmgr upgrade, reboot) with
the guards it leaves implicit made explicit. The install runs on the node as a
transient systemd unit, so a dropped ssh session or a restarted controller does
not kill it; the controller polls the unit and its journal. Every step's stdout
is kept verbatim under the run directory, plus a JSON state the GUI renders.

Steps per node: check → install → restart → verify. A failure holds the run
where it is and attaches what it saw; nothing retries on its own."""

from __future__ import annotations

import json
import shlex
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import ssh

APT_TIMEOUT_S = 60 * 60
FW_TIMEOUT_S = 25 * 60          # the Dashboard's own firmware budget
REBOOT_TIMEOUT_S = 30 * 60      # a reboot that flashes firmware capsules took ~15 min on a GX10
MIN_DISK_FREE = 5 * 1024 ** 3

# Priors for the time estimate, refined from real runs (see _learn_durations).
# Per package covers download + unpack + configure; firmware and restart are
# typical figures, well inside NVIDIA's 25-minute firmware budget.
DEFAULT_DURATIONS = {"apt_per_pkg": 1.5, "apt_fixed": 60.0, "fw": 60.0, "reboot": 15 * 60.0, "verify": 45.0}

APPLY_SCRIPT = r"""#!/bin/bash
# spark-fleet-updates apply — NVIDIA's documented sequence, non-interactive.
set -o pipefail
export DEBIAN_FRONTEND=noninteractive
echo "::phase lock"
for i in $(seq 1 120); do fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || break; sleep 5; done
echo "::phase apt-update"
apt-get -o DPkg::Lock::Timeout=600 update || { echo "::fail apt-update rc=$?"; exit 10; }
echo "::phase apt-upgrade"
apt-get -o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
        full-upgrade -y || { echo "::fail apt-upgrade rc=$?"; exit 11; }
echo "::phase fw-refresh"
fwupdmgr refresh --force; rc=$?
[ $rc -eq 0 ] || [ $rc -eq 2 ] || echo "fwupdmgr refresh rc=$rc (continuing)"
echo "::phase fw-upgrade"
fwupdmgr upgrade -y --no-reboot-check; rc=$?
if [ $rc -ne 0 ] && [ $rc -ne 2 ]; then echo "::fail fw-upgrade rc=$rc"; exit 12; fi
echo "::done"
"""

# The same shape with nothing inside it: exercises systemd-run, the journal
# follow, progress counting and result reading without touching a package.
REHEARSAL_SCRIPT = r"""#!/bin/bash
echo "::phase lock"; sleep 2
echo "::phase apt-update"; echo "Hit:1 rehearsal — no repositories contacted"; sleep 2
echo "::phase apt-upgrade"
for p in one two three four; do echo "Unpacking rehearsal-$p (0.0) ..."; sleep 1; echo "Setting up rehearsal-$p (0.0) ..."; done
echo "::phase fw-refresh"; echo "rehearsal — fwupdmgr not run"; sleep 1
echo "::phase fw-upgrade"; echo "rehearsal — fwupdmgr not run"; sleep 1
echo "::done"
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Run:
    """One update run: one or more nodes, sequentially."""

    def __init__(self, run_id: str, run_dir: Path, nodes: list[dict], service, rehearse: bool = False,
                 password: str | None = None):
        self.rehearse = rehearse
        # Held in memory for this run only — never written to state.json, the log
        # or an envelope. With it, privileged commands run through `sudo -S`;
        # without it, `sudo -n` (the Spark grants this user passwordless sudo).
        self._password = password or None
        self.durations = dict(DEFAULT_DURATIONS)
        try:
            learned = json.loads((run_dir.parent / "durations.json").read_text())
            self.durations.update({k: float(v) for k, v in learned.items() if k in DEFAULT_DURATIONS})
        except (OSError, ValueError):
            pass
        self.id = run_id
        self.dir = run_dir
        self.nodes = nodes                    # inventory entries, in order
        self.svc = service                    # for collect/score and inventory
        self.stop_requested = False
        self.thread: threading.Thread | None = None
        self.state = {
            "id": run_id, "started_at": _now(), "finished_at": None, "rehearsal": rehearse,
            "status": "running", "message": "",
            "nodes": [{"name": n["name"], "status": "queued", "step": None, "steps": {}, "progress": None,
                       "before": None, "after": None, "changed": None, "eta_seconds": None} for n in nodes],
        }
        self._lock = threading.Lock()
        self.dir.mkdir(parents=True, exist_ok=True)
        self._save()

    # ── privilege ────────────────────────────────────────────────────────
    def _run_root(self, host: str, user: str | None, command: str, timeout: int = 60) -> ssh.Result:
        """Run `command` as root on the node, the way the Spark allows."""
        if self._password:
            r = ssh.run(host, f"sudo -S -p '' {command}", user=user, stdin=self._password + "\n", timeout=timeout)
            if "Sorry, try again" in r.err or "incorrect password" in r.err.lower():
                return ssh.Result(r.rc or 1, r.out, "the password was not accepted")
            return r
        return ssh.run(host, f"sudo -n {command}", user=user, timeout=timeout)

    # ── bookkeeping ──────────────────────────────────────────────────────
    def _save(self) -> None:
        with self._lock:
            tmp = self.dir / "state.tmp"
            tmp.write_text(json.dumps(self.state, indent=1))
            tmp.replace(self.dir / "state.json")

    def _node(self, name: str) -> dict:
        return next(n for n in self.state["nodes"] if n["name"] == name)

    def _log(self, name: str, line: str) -> None:
        with (self.dir / f"{name}.log").open("a") as f:
            f.write(f"{datetime.now().strftime('%H:%M:%S')} {line.rstrip()}\n")

    def _step(self, name: str, step: str, status: str, **fields) -> None:
        n = self._node(name)
        n["step"] = step
        s = n["steps"].setdefault(step, {"started_at": _now()})
        s["status"] = status
        s.update(fields)
        if status in ("ok", "failed", "held"):
            s["finished_at"] = _now()
        self._save()

    def _record(self, name: str, step: str, tool: str, res: ssh.Result, t0: float, summary: str) -> None:
        """The orchestrator envelope the framework asks for, plus raw stdout."""
        env = {"tool": tool, "ts": _now(), "host": name, "status": "ok" if res.ok else "failed",
               "rc": res.rc, "duration_ms": int((time.time() - t0) * 1000), "summary": summary,
               "warnings": [res.err.strip()] if res.err.strip() else [],
               "artifacts": [f"{name}.{step}.out"]}
        (self.dir / f"{name}.{step}.json").write_text(json.dumps(env, indent=1))
        with (self.dir / f"{name}.{step}.out").open("a") as f:
            f.write(res.out)

    def _eta(self, name: str, seconds: float | None) -> None:
        self._node(name)["eta_seconds"] = None if seconds is None else max(0, int(seconds))

    def finish_late(self) -> None:
        """A run held at the restart whose node has since come back: settle it."""
        if all(x["status"] == "ok" for x in self.state["nodes"]):
            self.state["status"] = "ok"
            self.state["message"] = "updated " + ", ".join(x["name"] for x in self.state["nodes"]) + " (came back after the wait)"
        self.state["finished_at"] = _now()
        self._save()

    def _learn_durations(self, name: str, total_pkgs: int) -> None:
        """After a real, successful run: remember how long each step took."""
        if self.rehearse:
            return
        st = self._node(name)["steps"]
        def secs(step):
            d = st.get(step, {})
            try:
                a = datetime.strptime(d["started_at"], "%Y-%m-%dT%H:%M:%SZ"); b = datetime.strptime(d["finished_at"], "%Y-%m-%dT%H:%M:%SZ")
                return (b - a).total_seconds()
            except (KeyError, ValueError):
                return None
        learned = dict(self.durations)
        apt = st.get("install", {}).get("apt_seconds")
        if apt and total_pkgs:
            learned["apt_per_pkg"] = round(max(1.0, (apt - learned["apt_fixed"]) / total_pkgs), 2)
        fw = st.get("install", {}).get("fw_seconds")
        if fw:
            learned["fw"] = round(max(30.0, fw), 1)
        rb = secs("restart")
        if st.get("restart", {}).get("late"):
            rb = None   # we do not know how long it really took
        if rb:
            learned["reboot"] = round(max(30.0, rb), 1)
        vf = secs("verify")
        if vf:
            learned["verify"] = round(max(15.0, vf), 1)
        try:
            (self.dir.parent / "durations.json").write_text(json.dumps(learned, indent=1))
        except OSError:
            pass

    def _hold(self, name: str, step: str, why: str) -> None:
        self._log(name, f"HOLD at {step}: {why}")
        self._step(name, step, "failed", error=why)
        n = self._node(name)
        n["status"] = "failed"
        self.state["status"] = "failed"
        self.state["message"] = f"{name}: {why}"
        self._save()

    # ── the state machine ────────────────────────────────────────────────
    def start(self) -> None:
        self.thread = threading.Thread(target=self._main, name=f"run-{self.id}", daemon=True)
        self.thread.start()

    def _main(self) -> None:
        try:
            for node in self.nodes:
                if self.stop_requested:
                    self.state["status"] = "stopped"; break
                ok = self._update_one(node)
                if not ok:
                    break
            else:
                self.state["status"] = "ok"
                self.state["message"] = ("rehearsal passed on " if self.rehearse else "updated ") + ", ".join(n["name"] for n in self.nodes)
        except Exception as e:  # keep the record even if the controller has a bug
            self.state["status"] = "failed"
            self.state["message"] = f"controller error: {e!r}"
        self.state["finished_at"] = _now()
        self._save()

    def _update_one(self, node: dict) -> bool:
        try:
            return self._update_one_inner(node)
        finally:
            # the check or the install may have held, or a stop may have been
            # asked, after the Dashboard was paused: it is never left that way
            self._resume_dashboard(node)

    def _resume_dashboard(self, node: dict) -> None:
        """Put the DGX Dashboard's settings.json back as it was before this run
        paused its updater — the same bytes, or no file — so the Spark's own
        update checks and its Updates page come back. A no-op unless this run
        paused it; safe to call twice."""
        name, host, user = node["name"], node["host"], node.get("user")
        n = self._node(name)
        before = n.get("dashboard_settings_before")
        if not before:
            return
        f = "/opt/nvidia/dgx-dashboard/settings.json"
        if before.get("unknown"):
            cmd = "bash -c " + shlex.quote(
                f"python3 - {f} <<'PY'\nimport json,sys\np=sys.argv[1]\nd=json.load(open(p))\n"
                "d.setdefault('update',{})['enabled']=True\njson.dump(d,open(p,'w'),indent=2)\nPY")
        elif before.get("absent"):
            cmd = f"rm -f {f}"
        else:
            cmd = "bash -c " + shlex.quote(f"printf %s {shlex.quote(before.get('text') or '')} > {f}")
        r = self._run_root(host, user, cmd, timeout=30)
        if r.ok:
            self._log(name, "resumed the DGX Dashboard's updater")
            n["dashboard_settings_before"] = None
        else:
            self._log(name, "could not resume the DGX Dashboard's updater: " + (r.err or r.out).strip()[:200]
                      + f" — until it is, its Updates page says disabled; `sudo rm {f}` on the Spark undoes it")
        self._save()

    def _update_one_inner(self, node: dict) -> bool:
        name, host, user = node["name"], node["host"], node.get("user")
        n = self._node(name)
        n["status"] = "running"
        self._save()

        # 1 ── check ────────────────────────────────────────────────────
        self._step(name, "check", "running")
        self._log(name, "checking that it is safe to update")
        t0 = time.time()
        posture = self.svc.collect_node(node, reason=f"run {self.id} check")
        if not posture or not posture.get("reachable"):
            self._hold(name, "check", "could not reach the Spark"); return False
        n["before"] = {"release": posture["release"]["on_name"], "updates": posture["updates"]["total"],
                       "behind": len(posture["release"]["behind"]), "boot_id": posture.get("boot_id")}
        problems = []
        if posture["reboot"]["blocking_inhibitors"]:
            problems.append("something is blocking a restart: " + "; ".join(posture["reboot"]["blocking_inhibitors"])[:200])
        if posture.get("dpkg_lock_held"):
            problems.append("another install is running")
        if (posture.get("disk_free_bytes") or 0) < MIN_DISK_FREE:
            problems.append("less than 5 GB of disk free")
        if posture.get("recipes_match") is False:
            problems.append("the Spark's release list differs from this tool's copy")
        if problems:
            self._hold(name, "check", "; ".join(problems)); return False
        # prove we can act as root before anything happens; -k ignores a cached grant
        pr = self._run_root(host, user, "-k true", timeout=30)
        if not pr.ok:
            why = ("the password was not accepted" if self._password
                   else "sudo needs a password on this Spark — press Update again and enter it")
            self._hold(name, "check", why); return False
        self._log(name, "root access: " + ("your password, for this run only" if self._password else "passwordless sudo on this Spark"))
        if posture.get("dashboard_auto_update") and self.rehearse:
            self._log(name, "rehearsal: would pause the DGX Dashboard's updater here (not touched)")
        elif posture.get("dashboard_auto_update"):
            # The flag does more than stop the Dashboard installing: with it set
            # the Dashboard stops CHECKING, and its Updates page reads "disabled
            # by your administrator". So it is a pause, not a setting: the file's
            # previous contents (or its absence) are kept in the run's state and
            # put back the moment the install unit ends, whatever happened.
            self._log(name, "pausing the DGX Dashboard's updater so two installers never run at once")
            r = self._run_root(host, user, "bash -c " + shlex.quote(
                "mkdir -p /opt/nvidia/dgx-dashboard && f=/opt/nvidia/dgx-dashboard/settings.json; "
                "python3 - \"$f\" <<'PY'\nimport json,sys,os\np=sys.argv[1]\nd={}\n"
                "before=open(p).read() if os.path.exists(p) else None\n"
                "try:\n d=json.loads(before) if before else {}\nexcept Exception:\n d={}\n"
                "if not isinstance(d,dict): d={}\nd.setdefault('update',{})['enabled']=False\n"
                "json.dump(d,open(p,'w'),indent=2)\nprint('ok')\n"
                "print(json.dumps({'absent': before is None, 'text': before}))\nPY"), timeout=30)
            self._record(name, "check", "pause-dashboard-updater", r, t0, r.out.strip()[:80])
            if not r.ok:
                self._hold(name, "check", "could not pause the DGX Dashboard's updater: " + r.err.strip()[:200]); return False
            try:
                n["dashboard_settings_before"] = json.loads(r.out.strip().splitlines()[-1])
            except (ValueError, IndexError):
                n["dashboard_settings_before"] = {"unknown": True}   # resume flips the flag back in place
            self._save()
        D = self.durations
        est_total = D["apt_fixed"] + D["apt_per_pkg"] * (posture["updates"]["total"] or 0) + D["fw"] + D["reboot"] + D["verify"]
        self._eta(name, est_total)
        self._step(name, "check", "ok", summary=f"on {posture['release']['on_name']}, {posture['updates']['total']} updates")
        if self.stop_requested:
            self._step(name, "check", "held"); self.state["status"] = "stopped"; return False

        # 2 ── install ──────────────────────────────────────────────────
        self._step(name, "install", "running", phase="lock")
        unit = f"spark-apply-{self.id}"
        total_pkgs = 4 if self.rehearse else (posture["updates"]["total"] or 0)
        self._log(name, f"installing {total_pkgs} package updates, then firmware, as transient unit {unit}")
        t0 = time.time()
        script = REHEARSAL_SCRIPT if self.rehearse else APPLY_SCRIPT
        if self.rehearse:
            self._log(name, "rehearsal: the unit only echoes the phases; apt and fwupdmgr are not run")
        r = ssh.run(host, "cat > /tmp/spark-fleet-apply.sh && chmod +x /tmp/spark-fleet-apply.sh", user=user,
                    stdin=script, timeout=30)
        if not r.ok:
            self._hold(name, "install", "could not stage the install script: " + r.err.strip()[:200]); return False
        r = self._run_root(host, user, f"systemd-run --unit={unit} --description='spark-fleet-updates {self.id}' "
                                       f"-p RemainAfterExit=yes /bin/bash /tmp/spark-fleet-apply.sh", timeout=30)
        if not r.ok:
            self._hold(name, "install", "could not start the install: " + (r.err or r.out).strip()[:200]); return False
        self._log(name, "started; keeps going even if this page or this controller goes away")

        phase, seen, deadline = "lock", 0, time.time() + APT_TIMEOUT_S + FW_TIMEOUT_S
        cursor = ""
        result = None
        phase_t0 = {"lock": time.time()}
        apt_seconds = fw_seconds = None
        while time.time() < deadline:
            time.sleep(5)
            jr = self._run_root(host, user, f"journalctl -u {unit} --no-pager -o cat " + (f"--after-cursor={shlex.quote(cursor)} " if cursor else "")
                                + "--show-cursor", timeout=30)
            lines = jr.out.splitlines()
            for line in lines:
                if line.startswith("-- cursor: "):
                    cursor = line[len("-- cursor: "):].strip(); continue
                self._log(name, line)
                with (self.dir / f"{name}.install.out").open("a") as f:
                    f.write(line + "\n")
                if line.startswith("::phase ") or line.startswith("::done"):
                    new_phase = line.split()[1] if line.startswith("::phase ") else "done"
                    if new_phase != phase:
                        if phase == "apt-upgrade":
                            apt_seconds = time.time() - phase_t0.get("apt-upgrade", time.time())
                        if phase == "fw-upgrade":
                            fw_seconds = time.time() - phase_t0.get("fw-refresh", time.time())
                        phase = new_phase
                        phase_t0.setdefault(phase, time.time())
                elif line.startswith("Unpacking ") or line.startswith("Setting up "):
                    seen += 1
            progress = None
            if phase == "apt-upgrade" and total_pkgs:
                progress = min(0.95, seen / (2 * total_pkgs))          # unpack + setup per package
            elif phase in ("fw-refresh", "fw-upgrade"):
                progress = 1.0
            # time left: measured rate once the install is under way, priors before that
            now = time.time()
            if phase in ("lock", "apt-update"):
                remaining = D["apt_fixed"] + D["apt_per_pkg"] * total_pkgs + D["fw"] + D["reboot"] + D["verify"]
            elif phase == "apt-upgrade":
                frac = progress or 0.0
                spent = now - phase_t0.get("apt-upgrade", now)
                apt_left = spent * (1 - frac) / frac if frac > 0.05 else D["apt_per_pkg"] * total_pkgs
                remaining = apt_left + D["fw"] + D["reboot"] + D["verify"]
            elif phase in ("fw-refresh", "fw-upgrade"):
                remaining = max(30.0, D["fw"] - (now - phase_t0.get("fw-refresh", now))) + D["reboot"] + D["verify"]
            else:
                remaining = D["reboot"] + D["verify"]
            self._eta(name, remaining)
            self._step(name, "install", "running", phase=phase, progress=progress,
                       detail=(lines[-2] if len(lines) > 1 else "")[:140])
            sr = ssh.run(host, f"systemctl show {unit} -p ActiveState,SubState,Result,ExecMainStatus", user=user, timeout=20)
            props = dict(l.split("=", 1) for l in sr.out.splitlines() if "=" in l)
            if props.get("ActiveState") in ("inactive", "failed") or props.get("SubState") == "exited":
                result = props; break
        if result is None:
            self._hold(name, "install", "the install did not finish within its time budget; it may still be running on the Spark"); return False
        rc = int(result.get("ExecMainStatus", "1") or 1)
        self._run_root(host, user, f"bash -c 'systemctl stop {unit} >/dev/null 2>&1; systemctl reset-failed {unit} >/dev/null 2>&1; rm -f /tmp/spark-fleet-apply.sh'", timeout=20)
        self._resume_dashboard(node)
        env = ssh.Result(rc, "", "")
        self._record(name, "install", "apply.sh", env, t0, f"phase {phase}, rc {rc}")
        if rc != 0 or result.get("Result") not in ("success", None):
            self._hold(name, "install", f"the install failed during {phase} (exit {rc}); the log has the details"); return False
        self._step(name, "install", "ok", phase="done", progress=1.0, apt_seconds=apt_seconds, fw_seconds=fw_seconds)
        self._eta(name, D["reboot"] + D["verify"])
        if self.stop_requested:
            self._log(name, "stopped before the restart, as asked; the Spark has the new packages but has not restarted")
            self.state["status"] = "stopped"; return False

        # 3 ── restart ──────────────────────────────────────────────────
        self._step(name, "restart", "running")
        before_boot = posture.get("boot_id")
        if self.rehearse:
            br = ssh.run(host, "cat /proc/sys/kernel/random/boot_id", user=user, timeout=15)
            same = br.ok and br.out.strip() == before_boot
            self._log(name, f"rehearsal: restart skipped; boot_id readable and unchanged: {same}")
            if not same:
                self._hold(name, "restart", "could not read boot_id, which the real restart wait depends on"); return False
            self._step(name, "restart", "ok", seconds=0, skipped=True)
            self._eta(name, D["verify"])
            after = self.svc.collect_node(node, reason=f"run {self.id} verify")
            if not after or not after.get("reachable"):
                self._hold(name, "verify", "could not re-check the Spark"); return False
            n["after"] = {"release": after["release"]["on_name"], "updates": after["updates"]["total"],
                          "behind": len(after["release"]["behind"]), "boot_id": after.get("boot_id")}
            self._step(name, "verify", "ok", summary=f"re-checked: still on {after['release']['on_name']}, nothing changed", skipped=True)
            n["status"] = "ok"
            self._log(name, "rehearsal passed: every step ran except the install itself and the restart")
            self._save()
            return True
        self._log(name, "restarting")
        t0 = time.time()
        r = self._run_root(host, user, "systemctl reboot", timeout=30)
        # the connection dropping is the expected outcome; only an explicit refusal is a failure
        if r.rc not in (0, 255) and "closed" not in r.err.lower():
            self._record(name, "restart", "systemctl reboot", r, t0, "refused")
            self._hold(name, "restart", "the Spark refused to restart: " + (r.err or r.out).strip()[:200]); return False
        deadline = time.time() + REBOOT_TIMEOUT_S
        new_boot = None
        while time.time() < deadline:
            time.sleep(10)
            br = ssh.run(host, "cat /proc/sys/kernel/random/boot_id", user=user, timeout=15)
            if br.ok and br.out.strip() and br.out.strip() != before_boot:
                new_boot = br.out.strip(); break
            self._eta(name, max(30.0, D["reboot"] - (time.time() - t0)) + D["verify"])
            self._step(name, "restart", "running", detail=f"waiting for it to come back · {int(time.time() - t0)} s")
        if not new_boot:
            self._hold(name, "restart", "the Spark has not come back within 30 minutes — when it does, use \"it's back, verify now\""); return False
        self._log(name, f"back after {int(time.time() - t0)} s")
        self._step(name, "restart", "ok", seconds=int(time.time() - t0))

        # 4 ── verify ───────────────────────────────────────────────────
        return self.verify_node(node, settle=15)

    def verify_node(self, node: dict, settle: int = 0) -> bool:
        """The last step, also runnable on its own for a run that was held at the
        restart: did the update land? Success means nothing left to install and
        no restart pending. Whether NVIDIA's latest release is *reached* is
        reported, not gated — on an OEM board the vendor may not have published
        the release's firmware yet, and no amount of updating changes that."""
        name = node["name"]
        n = self._node(name)
        total_pkgs = (n.get("before") or {}).get("updates") or 0
        self._step(name, "verify", "running")
        self._eta(name, self.durations["verify"])
        if settle:
            time.sleep(settle)   # let services settle before scoring
        after = self.svc.collect_node(node, reason=f"run {self.id} verify")
        if not after or not after.get("reachable"):
            self._hold(name, "verify", "could not reach the Spark after the restart"); return False
        rel = after["release"]
        before = n.get("before") or {}
        if before.get("boot_id") and after.get("boot_id") == before.get("boot_id"):
            self._hold(name, "verify", "the Spark has not restarted since the install"); return False
        n["after"] = {"release": rel["on_name"], "updates": after["updates"]["total"],
                      "behind": len(rel["behind"]), "boot_id": after.get("boot_id")}
        fw_behind = [b for b in rel["behind"] if b["kind"] == "firmware"]
        n["changed"] = {"release": [before.get("release"), rel["on_name"]],
                        "updates": [before.get("updates"), after["updates"]["total"]],
                        "behind": [before.get("behind"), len(rel["behind"])],
                        "firmware_now": [{"label": b["label"], "installed": b["installed"], "target": b["target"]} for b in fw_behind]}
        problems = []
        if after["updates"]["total"]:
            problems.append(f"{after['updates']['total']} package updates still pending")
        if after["reboot"]["required"]:
            problems.append("a restart is still required")
        if after.get("firmware_updates"):
            problems.append(f"{len(after['firmware_updates'])} firmware updates still offered")
        if problems:
            self._step(name, "verify", "failed", error="; ".join(problems))
            n["status"] = "failed"; self.state["status"] = "failed"
            self.state["message"] = f"{name}: " + "; ".join(problems); self._save()
            return False
        note = ""
        if rel["available"]:
            note = (f"on {rel['on_name']}; {rel['latest_name']} still needs firmware "
                    f"({', '.join(b['label'] for b in fw_behind) or 'components'}) the vendor has not published for this board yet")
            n["release_note"] = note
        self._step(name, "verify", "ok", summary=note or f"on {rel['on_name']}, nothing left to install")
        if "restart" in n["steps"] and n["steps"]["restart"].get("status") != "ok":
            n["steps"]["restart"]["status"] = "ok"; n["steps"]["restart"]["late"] = True
            n["steps"]["restart"]["finished_at"] = n["steps"]["restart"].get("finished_at") or _now()
        self._eta(name, 0)
        n["status"] = "ok"
        self._log(name, "done: " + (note or f"on {rel['on_name']}, nothing left to install"))
        self._learn_durations(name, total_pkgs)
        self._save()
        return True
