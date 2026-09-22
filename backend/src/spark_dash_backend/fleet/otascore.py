#!/usr/bin/env python3
"""Off-box port of NVIDIA's nvidia-spark-ota-check scorer.

Reimplements the scoring in `ota_checker.py`, `check_packages.py`,
`check_software.py` and `check_firmware.py` (see
`reference/node-sparky/spark-ota-check/`) so that it runs on the control host
against facts collected over SSH, instead of on the node as root.

Two things this file deliberately owns rather than shells out for:

- **Debian version comparison.** NVIDIA calls `dpkg --compare-versions`. A
  control host is not necessarily a Debian box, so `deb_compare` implements
  dpkg's algorithm directly. `validate_against_nodes.py` checks it against a
  real `dpkg` on a node.
- **Recipe loading.** The recipes are world-readable on every node; we carry a
  copy so the fleet view answers "what is newest" with every node offline.

Fidelity notes, each of which is a place it would be easy to get wrong:

- `package_results` is computed WITHOUT ceilings; the ranged (ceiling-aware)
  results feed `match_*` and `detection_rank`. NVIDIA computes both.
- An empty component list scores 100.0, not 0.0.
- Firmware matching is a regex substring test with digit/dot boundaries, not a
  version comparison.
- `_is_nvidia_fe()` picks the firmware path, and it is NOT constant across a
  fleet: NVIDIA-built boards merge fwupd with dmidecode, OEM boards use
  dmidecode alone.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DETECTION_FW_WEIGHT = 0.35
DETECTION_SW_WEIGHT = 0.25
DETECTION_PKG_WEIGHT = 0.40
TORN_THRESHOLD_PCT = 70

_COMPONENT_ACTIONS = {
    "firmware": "fwupdmgr refresh ; fwupdmgr upgrade",
    "software": "apt update ; apt full-upgrade",
    "packages": "apt update ; apt full-upgrade",
}


# ── Debian version comparison (dpkg --compare-versions, in Python) ───────────

def _order(c: str) -> int:
    """dpkg's character ordering: '~' first, then end-of-string, then letters,
    then everything else."""
    if c.isdigit():
        return 0
    if c.isalpha():
        return ord(c)
    if c == "~":
        return -1
    if c:
        return ord(c) + 256
    return 0


def _cmp_fragment(a: str, b: str) -> int:
    i = j = 0
    while i < len(a) or j < len(b):
        first_diff = 0
        while (i < len(a) and not a[i].isdigit()) or (j < len(b) and not b[j].isdigit()):
            ac = _order(a[i]) if i < len(a) else 0
            bc = _order(b[j]) if j < len(b) else 0
            if ac != bc:
                return ac - bc
            i += 1
            j += 1
        while i < len(a) and a[i] == "0":
            i += 1
        while j < len(b) and b[j] == "0":
            j += 1
        while i < len(a) and a[i].isdigit() and j < len(b) and b[j].isdigit():
            if first_diff == 0:
                first_diff = ord(a[i]) - ord(b[j])
            i += 1
            j += 1
        if i < len(a) and a[i].isdigit():
            return 1
        if j < len(b) and b[j].isdigit():
            return -1
        if first_diff:
            return first_diff
    return 0


def _split_version(v: str) -> tuple[int, str, str]:
    epoch = 0
    rest = v
    if ":" in v:
        head, _, tail = v.partition(":")
        if head.isdigit():
            epoch = int(head)
            rest = tail
    if "-" in rest:
        upstream, _, revision = rest.rpartition("-")
    else:
        upstream, revision = rest, ""
    return epoch, upstream, revision


def deb_compare(a: str, b: str) -> int:
    """Return <0, 0, >0 as dpkg would order versions *a* and *b*."""
    ae, au, ar = _split_version(a)
    be, bu, br = _split_version(b)
    if ae != be:
        return -1 if ae < be else 1
    r = _cmp_fragment(au, bu)
    if r:
        return r
    return _cmp_fragment(ar, br)


def deb_ge(a: str, b: str) -> bool:
    return deb_compare(a, b) >= 0


def deb_lt(a: str, b: str) -> bool:
    return deb_compare(a, b) < 0


def deb_gt(a: str, b: str) -> bool:
    return deb_compare(a, b) > 0


# ── Results and recipes ──────────────────────────────────────────────────────

@dataclass
class CheckResult:
    name: str
    expected: str
    found: str
    passed: bool
    message: str


def make_result(name: str, expected: str, found: str) -> CheckResult:
    """NVIDIA's utils.make_result: a bounded substring match, not a version compare."""
    if not found:
        return CheckResult(name, expected, "", False, f"ERROR: {name}: <missing> != {expected}")
    if re.search(r"(?<![.\d])" + re.escape(expected) + r"(?![.\d])", found):
        return CheckResult(name, expected, found, True, f"- {name} match: {found}")
    return CheckResult(name, expected, found, False,
                       f"ERROR: {name} mismatch: {found} != {expected}")


@dataclass
class OTARecipe:
    path: Path
    metadata: dict
    packages: list[dict]
    firmware: list[dict]
    connectx: list[dict]
    software: list[dict]

    @property
    def name(self) -> str:
        return self.metadata.get("name", "UNKNOWN")

    @property
    def external_name(self) -> str:
        return self.metadata.get("external_name") or self.name

    @property
    def description(self) -> str:
        return self.metadata.get("description", "")

    @property
    def release_notes_url(self) -> str:
        return self.metadata.get("releaseNotesUrl", "")

    @property
    def required_match(self) -> list[str]:
        return self.metadata.get("required_match", [])

    @property
    def release_date_str(self) -> str:
        return self.metadata.get("releaseDate", "")

    @property
    def release_date(self) -> datetime:
        if not self.release_date_str:
            return datetime.min.replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(self.release_date_str.replace("Z", "+00:00"))

    @property
    def is_ebeta(self) -> bool:
        return "ebeta" in self.name.lower()


def load_recipes(directory: Path) -> list[OTARecipe]:
    """Load every spark-ota-*.json, newest first."""
    recipes = []
    for p in sorted(directory.glob("spark-ota-*.json")):
        data = json.loads(p.read_text())
        recipes.append(OTARecipe(
            path=p,
            metadata=data.get("metadata", {}),
            packages=data.get("package", []),
            firmware=data.get("firmware", []),
            connectx=data.get("connectx", []),
            software=data.get("software", []),
        ))
    recipes.sort(key=lambda r: r.release_date, reverse=True)
    return recipes


# ── Node facts: everything the scorer needs from one machine ─────────────────

@dataclass
class NodeFacts:
    """Inputs the scorer consumes. `privileged` says whether the root-only
    sources (dmidecode, mstflint) were actually collected."""
    host: str
    packages: dict[str, str]          # dpkg-query -W
    kernel: str                       # uname -r
    driver: str                       # nvidia-smi -q, "Driver Version" line
    fwupd_devices: dict[str, str]     # guid -> version, from fwupdmgr get-devices --json
    dmidecode_t45: str = ""           # sudo dmidecode -t 45
    connectx_fw: str = ""             # sudo mstflint -d <addr> q
    board_vendor: str = ""            # /sys/class/dmi/id/board_vendor

    @property
    def is_nvidia_fe(self) -> bool:
        return "NVIDIA" in self.board_vendor.upper()


def parse_dpkg_query(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if "," in line:
            name, version = line.split(",", 1)
            out[name] = version
    return out


def _translate_hex_version(version: str) -> str:
    try:
        v = int(version.strip(), 0)
        return f"{(v >> 24) & 0xFF}.{(v >> 8) & 0xFFFF}.{v & 0xFF}"
    except (ValueError, TypeError):
        return version


def parse_fwupd_devices(text: str) -> dict[str, str]:
    guid_to_version: dict[str, str] = {}
    if not text.strip():
        return guid_to_version
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return guid_to_version
    for device in data.get("Devices", []):
        version = device.get("Version", "")
        if version.startswith("0x"):
            version = _translate_hex_version(version)
        for guid in device.get("Guid", []):
            guid_to_version[guid] = version
    return guid_to_version


# ── Component checks ─────────────────────────────────────────────────────────

def check_packages(packages: list[dict], installed: dict[str, str],
                   ceilings: dict[str, str] | None = None) -> list[CheckResult]:
    results = []
    for pkg in packages:
        name, req_ver = pkg["name"], pkg["version"]
        ceiling = ceilings.get(name) if ceilings else None
        inst_ver = installed.get(name, "")
        if not inst_ver:
            results.append(CheckResult(name, req_ver, "", False,
                                       f"ERROR: {name} not found, expected >= {req_ver}"))
        elif deb_ge(inst_ver, req_ver) and (ceiling is None or deb_lt(inst_ver, ceiling)):
            results.append(CheckResult(name, req_ver, inst_ver, True,
                                       f"- {name} match: {inst_ver}"))
        elif not deb_ge(inst_ver, req_ver):
            results.append(CheckResult(name, req_ver, inst_ver, False,
                                       f"ERROR: {name} version too low: {inst_ver} < {req_ver}"))
        else:
            results.append(CheckResult(name, req_ver, inst_ver, False,
                                       f"INFO: {name} version beyond this OTA: {inst_ver} >= {ceiling}"))
    return results


def _version_tuple(ver: str) -> tuple:
    parts = re.split(r"[.\-]", ver)
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts)


def _sw_version_ge(installed: str, required: str) -> bool:
    return _version_tuple(installed) >= _version_tuple(required)


def check_software(software: list[dict], facts: NodeFacts) -> list[CheckResult]:
    results = []
    for item in software:
        name, expected = item["name"], item["version"]
        if name == "kernel":
            found = facts.kernel
            passed = _sw_version_ge(found, expected)
            results.append(CheckResult("kernel", expected, found, passed,
                                       f"- kernel match: {found}" if passed
                                       else f"ERROR: kernel mismatch: {found} < {expected}"))
        elif name == "driver":
            found = facts.driver
            if not found:
                results.append(CheckResult("driver", expected, "", False,
                                           "WARN: driver version unavailable (nvidia-smi)"))
                continue
            passed = _sw_version_ge(found, expected)
            results.append(CheckResult("driver", expected, found, passed,
                                       f"- driver match: {found}" if passed
                                       else f"ERROR: driver mismatch: {found} < {expected}"))
    return results


def _filter_firmware(firmware: list[dict], devices: dict[str, str]) -> list[dict]:
    filtered = list(firmware)
    tpm_guid = next((fw["guid"] for fw in firmware if fw["name"] == "TPM"), "")
    if tpm_guid and tpm_guid not in devices:
        filtered = [fw for fw in filtered if fw["name"] != "TPM"]
    ec_guid = next((fw["guid"] for fw in filtered if fw["name"] == "EC"), "")
    ec_unfused_guid = next((fw["guid"] for fw in filtered if fw["name"] == "EC Unfused"), "")
    if ec_guid and ec_unfused_guid:
        if ec_guid in devices:
            filtered = [fw for fw in filtered if fw["name"] != "EC Unfused"]
        elif ec_unfused_guid in devices:
            filtered = [fw for fw in filtered if fw["name"] != "EC"]
        else:
            filtered = [fw for fw in filtered if fw["name"] != "EC Unfused"]
    return filtered


def _dmidecode_section_for(name: str) -> str:
    if name == "SOCFW":
        return "FLASH"
    if name in ("EC", "EC Unfused"):
        return "EC Firmware"
    return name


def _check_dmidecode_section(section: str, name: str, expected: str, output: str) -> CheckResult:
    current = None
    for line in output.splitlines():
        if "Firmware Component Name:" in line:
            current = line.split(":", 1)[1].strip()
        elif "Firmware Version:" in line and current == section:
            return make_result(name, expected, line.split(":", 1)[1].strip())
    return make_result(name, expected, "")


def _check_usbpd(name: str, expected: str, output: str) -> CheckResult:
    exp_ver = expected.removeprefix("0.")
    pd0 = pd1 = ""
    for line in output.splitlines():
        s = line.strip()
        if "PD0" in s:
            pd0 = s
        if "PD1" in s:
            pd1 = s
    if pd0 and pd1 and exp_ver in pd0 and exp_ver in pd1:
        return CheckResult(name, expected, f"PD0/PD1 {exp_ver}", True,
                           f"- {name} check passed. Version: PD0/PD1 {exp_ver}")
    if not pd0 and not pd1:
        return make_result(name, expected, "")
    found = f"PD0={pd0 or '<missing>'} PD1={pd1 or '<missing>'}"
    return CheckResult(name, expected, found, False,
                       f"ERROR: {name} mismatch: {found} != {expected}")


def _check_firmware_dmidecode(firmware: list[dict], output: str) -> list[CheckResult]:
    results = []
    for fw in firmware:
        name, expected = fw["name"], fw["version"]
        if name == "USBPD":
            results.append(_check_usbpd(name, expected, output))
        else:
            results.append(_check_dmidecode_section(
                _dmidecode_section_for(name), name, expected, output))
    return results


def _check_firmware_fwupd(firmware: list[dict], devices: dict[str, str]) -> list[CheckResult]:
    return [make_result(fw["name"], fw["version"], devices.get(fw.get("guid", ""), ""))
            for fw in firmware]


def _merge_fwupd_dmidecode(fwupd_results: list[CheckResult],
                           dmi_results: list[CheckResult]) -> list[CheckResult]:
    dmi_by_name = {r.name: r for r in dmi_results}
    merged = []
    for fw in fwupd_results:
        dmi = dmi_by_name.get(fw.name)
        if dmi is None:
            merged.append(fw)
            continue
        passed = fw.passed and dmi.passed
        found = f"fwupd={fw.found or '<missing>'}; dmidecode={dmi.found or '<missing>'}"
        message = (f"- {fw.name} match (fwupd + dmidecode): {dmi.found}" if passed
                   else f"ERROR: {fw.name} mismatch: {found} != {fw.expected} "
                        f"(fwupd passed={fw.passed}, dmidecode passed={dmi.passed})")
        merged.append(CheckResult(fw.name, fw.expected, found, passed, message))
    return merged


def check_firmware(firmware: list[dict], connectx: list[dict], facts: NodeFacts,
                   fwupd_only: bool = False) -> list[CheckResult]:
    """Firmware scoring. `fwupd_only` is the unprivileged variant: it drops the
    dmidecode half that NVIDIA requires, and is a deviation, not a shortcut."""
    results: list[CheckResult] = []
    if firmware:
        if fwupd_only:
            results.extend(_check_firmware_fwupd(
                _filter_firmware(firmware, facts.fwupd_devices), facts.fwupd_devices))
        elif facts.is_nvidia_fe:
            fw_to_check = _filter_firmware(firmware, facts.fwupd_devices)
            results.extend(_merge_fwupd_dmidecode(
                _check_firmware_fwupd(fw_to_check, facts.fwupd_devices),
                _check_firmware_dmidecode(fw_to_check, facts.dmidecode_t45)))
        else:
            results.extend(_check_firmware_dmidecode(firmware, facts.dmidecode_t45))

    if connectx:
        cx = connectx[0]
        found = ""
        for line in facts.connectx_fw.splitlines():
            if "FW Version:" in line:
                found = line.split()[-1].strip()
                break
        if found:
            results.append(make_result(cx["name"], cx["version"], found))
    return results


# ── Scoring ──────────────────────────────────────────────────────────────────

@dataclass
class OTAScore:
    recipe: OTARecipe
    package_results: list[CheckResult]
    software_results: list[CheckResult]
    firmware_results: list[CheckResult]
    match_pct: float = 0.0
    match_failed: list[str] = field(default_factory=list)
    match_pkg_pct: float = 0.0
    match_sw_pct: float = 0.0
    match_fw_pct: float = 0.0

    @property
    def required_satisfied(self) -> bool:
        req = self.recipe.required_match
        if not req:
            return True
        return not any(name in self.match_failed for name in req)

    @property
    def all_results(self) -> list[CheckResult]:
        return self.package_results + self.software_results + self.firmware_results

    @property
    def total_checks(self) -> int:
        return len(self.all_results)

    @property
    def passed_checks(self) -> int:
        return sum(1 for r in self.all_results if r.passed)

    @property
    def torn_pct(self) -> float:
        if self.total_checks == 0:
            return 0.0
        return ((self.total_checks - self.passed_checks) / self.total_checks) * 100

    @staticmethod
    def _torn(results: list[CheckResult]) -> float:
        if not results:
            return 0.0
        return (sum(1 for r in results if not r.passed) / len(results)) * 100

    @property
    def torn_fw_pct(self) -> float:
        return self._torn(self.firmware_results)

    @property
    def torn_sw_pct(self) -> float:
        return self._torn(self.software_results)

    @property
    def torn_pkg_pct(self) -> float:
        return self._torn(self.package_results)

    @property
    def failed_names(self) -> list[str]:
        return [r.name for r in self.all_results if not r.passed]

    @property
    def detection_rank(self) -> float:
        return (DETECTION_FW_WEIGHT * self.match_fw_pct
                + DETECTION_SW_WEIGHT * self.match_sw_pct
                + DETECTION_PKG_WEIGHT * self.match_pkg_pct)


def _build_ceilings(recipes: list[OTARecipe]) -> dict[str, dict[str, str]]:
    by_date = sorted(recipes, key=lambda r: r.release_date)
    all_ceilings: dict[str, dict[str, str]] = {}
    for i, recipe in enumerate(by_date):
        ceilings: dict[str, str] = {}
        for comp in recipe.packages:
            name, ver = comp["name"], comp["version"]
            for j in range(i + 1, len(by_date)):
                newer = next((c["version"] for c in by_date[j].packages
                              if c["name"] == name), None)
                if newer and deb_gt(newer, ver):
                    ceilings[name] = newer
                    break
        all_ceilings[recipe.name] = ceilings
    return all_ceilings


def _pct(results: list[CheckResult]) -> float:
    if not results:
        return 0.0
    return (sum(1 for r in results if r.passed) / len(results)) * 100


def evaluate_recipe(recipe: OTARecipe, facts: NodeFacts,
                    pkg_ceilings: dict[str, str] | None = None,
                    fwupd_only: bool = False) -> OTAScore:
    pkg_ranged = check_packages(recipe.packages, facts.packages, ceilings=pkg_ceilings)
    pkg_results = check_packages(recipe.packages, facts.packages)
    sw_results = check_software(recipe.software, facts)
    fw_results = check_firmware(recipe.firmware, recipe.connectx, facts, fwupd_only=fwupd_only)

    match_all = pkg_ranged + sw_results + fw_results
    return OTAScore(
        recipe=recipe,
        package_results=pkg_results,
        software_results=sw_results,
        firmware_results=fw_results,
        match_pct=_pct(match_all),
        match_failed=[r.name for r in match_all if not r.passed],
        match_pkg_pct=_pct(pkg_ranged) if pkg_ranged else 100.0,
        match_sw_pct=_pct(sw_results) if sw_results else 100.0,
        match_fw_pct=_pct(fw_results) if fw_results else 100.0,
    )


def get_all_scores(recipes: list[OTARecipe], facts: NodeFacts,
                   fwupd_only: bool = False) -> list[OTAScore]:
    ceilings = _build_ceilings(recipes)
    scores = [evaluate_recipe(r, facts, ceilings.get(r.name), fwupd_only) for r in recipes]
    scores.sort(key=lambda s: (s.detection_rank, s.recipe.release_date), reverse=True)
    return scores


def get_detected(scores: list[OTAScore]) -> OTAScore:
    for s in scores:
        if s.required_satisfied:
            return s
    return scores[0]


# ── The three answers the fleet view needs ───────────────────────────────────

def installed_name(scores: list[OTAScore]) -> dict:
    best = get_detected(scores)
    return {"name": best.recipe.name, "releaseDate": best.recipe.release_date_str}


def torn_score(scores: list[OTAScore]) -> dict:
    best = get_detected(scores)
    return {"name": best.recipe.name, "releaseDate": best.recipe.release_date_str,
            "torn": round(best.torn_pct)}


def _recommended_actions(score: OTAScore) -> list[dict]:
    pairs = (("firmware", score.torn_fw_pct), ("software", score.torn_sw_pct),
             ("packages", score.torn_pkg_pct))
    return [{"component": c, "torn": round(t, 1), "action": _COMPONENT_ACTIONS[c]}
            for c, t in pairs if t > TORN_THRESHOLD_PCT]


def is_ota_available(scores: list[OTAScore]) -> dict:
    """Same verdict as `check_ota_status.py is-ota-available`, minus the
    self-update side effect that command has on the node."""
    detected = get_detected(scores)
    stable = [s for s in scores if not s.recipe.is_ebeta]
    if not stable:
        return {"available": False, "name": detected.recipe.external_name,
                "description": detected.recipe.description,
                "releaseNotesUrl": detected.recipe.release_notes_url,
                "releaseDate": detected.recipe.release_date_str,
                "metadataFilePath": "",
                "recommendedActions": _recommended_actions(detected)}
    latest = max(stable, key=lambda s: s.recipe.release_date)
    best_stable = detected if not detected.recipe.is_ebeta else get_detected(stable)
    available = best_stable.recipe.name != latest.recipe.name
    target = latest if available else best_stable
    return {"available": available, "name": target.recipe.external_name,
            "description": target.recipe.description,
            "releaseNotesUrl": target.recipe.release_notes_url,
            "releaseDate": target.recipe.release_date_str,
            "metadataFilePath": str(latest.recipe.path) if available else "",
            "recommendedActions": [] if available else _recommended_actions(detected)}


def summary(scores: list[OTAScore]) -> dict:
    best = get_detected(scores)
    return {
        "detected_ota": best.recipe.name,
        "releaseDate": best.recipe.release_date_str,
        "torn": round(best.torn_pct, 1),
        "scores": [
            {"ota": s.recipe.name, "releaseDate": s.recipe.release_date_str,
             "detection_rank": round(s.detection_rank, 2),
             "match": round(s.match_pct, 1), "torn": round(s.torn_pct, 1),
             "total_checks": s.total_checks, "passed_checks": s.passed_checks,
             "failed": s.failed_names,
             **({"required_match": s.recipe.required_match,
                 "required_satisfied": s.required_satisfied}
                if s.recipe.required_match else {})}
            for s in scores
        ],
    }
