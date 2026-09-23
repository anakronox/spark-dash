"""Turn one node's collected facts into the record the dashboard shows.

Two questions, answered separately because they are different questions:
  * is this Spark on NVIDIA's latest release?   (the scorer, against carried recipes)
  * how many package updates are pending?       (apt, via the simulation it ran)
and, for the reveal, exactly what an update would change."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from .otascore import (
    NodeFacts,
    get_all_scores,
    get_detected,
    is_ota_available,
    load_recipes,
    parse_dpkg_query,
    parse_fwupd_devices,
    summary,
)
from .vendors import platform_firmware

_INST = re.compile(r"^Inst (\S+)(?: \[([^\]]*)\])? \(([^ )]+) ([^)]*)\)")
_SUMMARY = re.compile(r"(\d+) upgraded, (\d+) newly installed, (\d+) to remove")
_KEPT_BACK = re.compile(r"^The following packages have been kept back:\n((?:^  .*\n)+)", re.M)


def _iso(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def parse_apt_sim(text: str) -> dict:
    """`apt-get -s full-upgrade` → the package list a person can read."""
    pkgs = []
    for line in text.splitlines():
        m = _INST.match(line)
        if not m:
            continue
        name, old, new, origin = m.groups()
        src = "nvidia" if "nvidia" in origin.lower() or "cuda" in origin.lower() else \
              "ubuntu-esm" if "esm" in origin.lower() else "ubuntu"
        pkgs.append({"name": name, "from": old or None, "to": new, "new": old is None,
                     "security": "security" in origin.lower(), "source": src})
    counts = {"upgraded": 0, "new": 0, "removed": 0}
    m = _SUMMARY.search(text)
    if m:
        counts = {"upgraded": int(m.group(1)), "new": int(m.group(2)), "removed": int(m.group(3))}
    # security-fix packages first, then new ones, then the rest, alphabetical within
    pkgs.sort(key=lambda p: (not p["security"], not p["new"], p["name"]))
    # What apt WOULD have installed and will not. A held package never reaches
    # the Inst lines, so this block is the only place the simulation admits it
    # exists -- and "0 updates" with a kept-back list is a different sentence
    # from "0 updates".
    m = _KEPT_BACK.search(text)
    kept = sorted(m.group(1).split()) if m else []
    return {"packages": pkgs, "counts": counts, "total": len(pkgs), "kept_back": kept}


def _hexver(v: str) -> str:
    if isinstance(v, str) and v.startswith("0x"):
        try:
            n = int(v, 16)
            return f"{(n >> 24) & 0xFF}.{(n >> 8) & 0xFFFF}.{n & 0xFF}"
        except ValueError:
            pass
    return v


def parse_fwupd_updates(text: str) -> list[dict]:
    """`fwupdmgr get-updates --json` → one row per device with an update."""
    if not text.strip():
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    rows = []
    for dev in data.get("Devices", []):
        rels = dev.get("Releases") or []
        if not rels:
            continue
        best = rels[0]
        rows.append({"device": dev.get("Name", "device"), "now": _hexver(dev.get("Version", "")),
                     "after": _hexver(best.get("Version", "")), "size": best.get("Size"),
                     "summary": best.get("Summary", ""), "vendor": dev.get("Vendor", ""),
                     "needs_reboot": "needs-reboot" in (dev.get("Flags") or [])})
    return rows


_FW_NAMES = {"SOCFW": "System firmware", "EC": "Embedded controller", "EC Unfused": "Embedded controller (unfused)",
             "USBPD": "USB-C power controller", "TPM": "TPM firmware", "CX7": "Network card (ConnectX-7)"}

# A firmware "version" the board reports that is not a version at all. The ASUS
# GX10 answers the TPM row of dmidecode -t 45 with `SOCTS`, so NVIDIA's TPM
# check can never pass there; that is "not reported by this board", not "behind".
_VERSIONISH = re.compile(r"\d+\.\d+")


def _firmware_gap(latest_score, fw_updates: list[dict], is_fe: bool, platform: dict) -> dict:
    """NVIDIA's firmware baseline against this board, and who can close the gap.

    The three states that matter to a person, kept apart on purpose:
      installable     fwupd is offering firmware — press Update
      pending-vendor  a partner board with nothing offered: the vendor has not
                      published NVIDIA's newer firmware yet. Not "outdated".
      pending-nvidia  an NVIDIA-built board with nothing offered — NVIDIA has
                      not pushed it to this board yet
    plus `current` when every firmware check passes."""
    outstanding, not_reported = [], []
    for r in latest_score.firmware_results:
        if r.passed:
            continue
        row = {"name": r.name, "label": _FW_NAMES.get(r.name, r.name),
               "installed": r.found or None, "target": r.expected}
        if r.found and not _VERSIONISH.search(r.found):
            row["not_reported"] = True
            not_reported.append(row)
        else:
            outstanding.append(row)
    if not outstanding:
        state = "current"
    elif fw_updates:
        state = "installable"
    elif not is_fe:
        state = "pending-vendor"
    else:
        state = "pending-nvidia"
    return {"state": state, "vendor": platform.get("vendor"), "outstanding": outstanding,
            "not_reported": not_reported}


def _software(scores, latest_score, apt_total) -> dict:
    """The software half of the release, judged on packages, kernel and driver
    alone — the part apt can move on any board, whoever built it.

    A failing check is one of two very different things, and they are kept
    apart: `behind` — installed, but older than the release wants, which an
    update should fix — and `missing` — not installed at all. Missing does not
    hold a release back: on sparky two station packages were removed on
    purpose, they are pulled in by a package the OTA metapackage does not
    depend on, and `full-upgrade` will never bring them back. Judging them as
    "behind" made a fully updated board read as stuck on OTA1.1, the last
    release that did not list them.

    `on` is the newest stable release whose software checks pass, missing
    packages excepted. `held` means something installed is too old and yet apt
    has nothing to install — a real anomaly worth a look."""
    def sw(s):
        return s.package_results + s.software_results

    def row(r):
        return {"kind": "software" if r in latest_score.software_results else "package",
                "name": r.name, "installed": r.found or None, "target": r.expected}
    behind = [row(r) for r in sw(latest_score) if not r.passed and r.found]
    missing = [row(r) for r in sw(latest_score) if not r.passed and not r.found]
    stable = sorted((s for s in scores if not s.recipe.is_ebeta),
                    key=lambda s: s.recipe.release_date, reverse=True)
    on = next((s for s in stable if all(r.passed or not r.found for r in sw(s))), None)
    state = "installable" if apt_total else ("held" if behind else "current")
    return {"state": state, "on": on.recipe.name if on else None,
            "on_name": on.recipe.external_name if on else None,
            "behind": behind, "missing": missing,
            # kept for readers of the earlier shape
            "failing": behind + missing}


def build(node: dict, facts: dict, recipes_dir: Path) -> dict:
    recipes = load_recipes(recipes_dir)
    nf = NodeFacts(host=node["name"], packages=parse_dpkg_query(facts.get("packages", "")),
                   kernel=facts.get("kernel", ""), driver=facts.get("driver", ""),
                   fwupd_devices=parse_fwupd_devices(facts.get("fwupd_devices", "")),
                   dmidecode_t45=facts.get("dmidecode_t45", ""), connectx_fw=facts.get("mstflint", ""),
                   board_vendor=facts.get("board_vendor", ""))
    scores = get_all_scores(recipes, nf)
    detected = get_detected(scores)
    avail = is_ota_available(scores)
    latest = max((r for r in recipes if not r.is_ebeta), key=lambda r: r.release_date)
    latest_score = next(s for s in scores if s.recipe.name == latest.name)

    behind = []
    for r in latest_score.all_results:
        if r.passed:
            continue
        kind = "firmware" if r in latest_score.firmware_results else "software" if r in latest_score.software_results else "package"
        behind.append({"kind": kind, "name": r.name, "label": _FW_NAMES.get(r.name, r.name),
                       "installed": r.found or None, "target": r.expected})

    if nf.connectx_fw:
        cx = next((r for r in latest_score.firmware_results if r.name == "CX7"), None)
        cx7 = {"state": "ok" if (cx and cx.passed) else "behind", "version": (cx.found if cx else "")}
    else:
        cx7 = {"state": "not-verifiable", "reason": "no cable plugged in — the card is switched off"}

    apt = parse_apt_sim(facts.get("apt_sim", ""))
    total, security = None, None
    if ";" in facts.get("apt_check", ""):
        a, b = facts["apt_check"].split(";")[:2]
        total, security = int(a), int(b)
    if total is None:
        total, security = apt["total"], sum(1 for p in apt["packages"] if p["security"])

    inhibitors_block = [l for l in facts.get("inhibitors", "").splitlines() if l.strip().endswith("block")]

    fw_updates = parse_fwupd_updates(facts.get("fwupd_updates", ""))
    platform = platform_firmware(facts.get("board_vendor", ""), facts.get("product_name", ""),
                                 facts.get("bios_version", ""), facts.get("bios_date", ""))
    gap = _firmware_gap(latest_score, fw_updates, nf.is_nvidia_fe, platform)
    software = _software(scores, latest_score, total)

    return {
        "name": node["name"], "host": node["host"], "reachable": True,
        "collected_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hostname": facts.get("hostname"), "board": facts.get("board_vendor"),
        "board_kind": "NVIDIA-built" if nf.is_nvidia_fe else "OEM-built",
        # the three lines the page shows, kept apart: what apt can move, what the
        # board's vendor has published, and what NVIDIA's baseline still wants
        "software": software,
        "platform_firmware": platform,
        "firmware_gap": gap,
        "kernel": facts.get("kernel"), "driver": facts.get("driver"), "boot_id": facts.get("boot_id"),
        "release": {
            "on": detected.recipe.name, "on_name": detected.recipe.external_name,
            "on_date": detected.recipe.release_date_str[:10],
            "latest": latest.name, "latest_name": latest.external_name, "latest_date": latest.release_date_str[:10],
            "available": bool(avail["available"]),
            "description": latest.description, "release_notes_url": latest.release_notes_url,
            "checks_passed": latest_score.passed_checks, "checks_total": latest_score.total_checks,
            "behind": behind,
        },
        "updates": {"total": total, "security": security, "as_of": _iso(facts.get("updates_available_mtime")),
                    "counts": apt["counts"], "packages": apt["packages"],
                    # AL6.3: state a person put on this node by hand. `held` is
                    # what they pinned; `kept_back` is what apt declined to
                    # install this time, which is usually the held set plus
                    # whatever depends on it.
                    "held": facts.get("apt_holds", []), "kept_back": apt.get("kept_back", [])},
        "firmware_updates": fw_updates,
        "cx7": cx7,
        "reboot": {"required": bool(facts.get("reboot_required")), "packages": facts.get("reboot_required_pkgs", []),
                   "blocking_inhibitors": inhibitors_block},
        "dashboard_auto_update": facts.get("dashboard_auto_update"),
        "sudo_passwordless": facts.get("sudo_passwordless"),
        "firmware_readable": facts.get("firmware_readable", True),
        "disk_free_bytes": facts.get("disk_free_bytes"), "dpkg_lock_held": facts.get("dpkg_lock_held"),
        "recipes_match": None,   # filled by the service, which knows the carried set
        "fabric": facts.get("fabric", {"macs": [], "ports": [], "lldp": [], "neigh": []}),
        "ota_check_version": facts.get("ota_check_version"),
        "collect_errors": facts.get("errors", {}),
        "scorer": summary(scores),
    }
