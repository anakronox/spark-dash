#!/usr/bin/env python3
"""Validate the off-box scorer against NVIDIA's own root checker.

Usage: uv run scripts/validate_fleet_scorer.py <collect-dir> <recipes-dir> [node ...]

MAINTAINER PATH, and it needs a fresh oracle: run the collector and NVIDIA's
own `check_ota_status.py` on each node, then point this at the results. The
scorer is a reimplementation of a root-only checker, and this is the only
thing that proves the reimplementation still agrees after either side moves.

<collect-dir>/<node>/ holds the facts collected over SSH plus the oracle
output (`oracle-summary.json` etc.) from the node's own root checker.

Reports, per node:
  * exact agreement on detected OTA, torn score and every per-recipe figure
  * what changes if the root-only inputs (dmidecode, mstflint) are withheld
"""

import json
import sys
from pathlib import Path

from spark_dash_backend.fleet.otascore import (
    NodeFacts,
    get_all_scores,
    installed_name,
    load_recipes,
    parse_dpkg_query,
    parse_fwupd_devices,
    summary,
    torn_score,
)


def read(p: Path) -> str:
    return p.read_text() if p.exists() else ""


def load_facts(host: str, d: Path) -> NodeFacts:
    driver_line = read(d / "driver.txt").strip()
    driver = ""
    if "Driver Version" in driver_line:
        driver = driver_line.split(":")[-1].strip()
    return NodeFacts(
        host=host,
        packages=parse_dpkg_query(read(d / "dpkg-query.txt")),
        kernel=read(d / "uname-r.txt").strip(),
        driver=driver,
        fwupd_devices=parse_fwupd_devices(read(d / "fwupd-devices.json")),
        dmidecode_t45=read(d / "dmidecode-45.txt"),
        connectx_fw=read(d / "mstflint.txt"),
        board_vendor=read(d / "board_vendor.txt").strip(),
    )


def compare_summary(ours: dict, oracle: dict) -> list[str]:
    diffs = []
    for key in ("detected_ota", "releaseDate", "torn"):
        if ours.get(key) != oracle.get(key):
            diffs.append(f"{key}: ours={ours.get(key)!r} oracle={oracle.get(key)!r}")

    ours_by_ota = {s["ota"]: s for s in ours["scores"]}
    oracle_by_ota = {s["ota"]: s for s in oracle["scores"]}
    if set(ours_by_ota) != set(oracle_by_ota):
        diffs.append(f"recipe sets differ: {set(ours_by_ota) ^ set(oracle_by_ota)}")

    for ota in sorted(set(ours_by_ota) & set(oracle_by_ota)):
        a, b = ours_by_ota[ota], oracle_by_ota[ota]
        for key in ("detection_rank", "match", "torn", "total_checks",
                    "passed_checks", "required_satisfied"):
            if (key in a or key in b) and a.get(key) != b.get(key):
                diffs.append(f"{ota}.{key}: ours={a.get(key)!r} oracle={b.get(key)!r}")
        only_ours = sorted(set(a.get("failed", [])) - set(b.get("failed", [])))
        only_oracle = sorted(set(b.get("failed", [])) - set(a.get("failed", [])))
        if only_ours or only_oracle:
            diffs.append(f"{ota}.failed: +{len(only_ours)} only-ours "
                         f"{only_ours[:6]}{'...' if len(only_ours) > 6 else ''}, "
                         f"-{len(only_oracle)} only-oracle "
                         f"{only_oracle[:6]}{'...' if len(only_oracle) > 6 else ''}")
    return diffs


def main() -> int:
    collect_dir = Path(sys.argv[1])
    recipes_dir = Path(sys.argv[2])
    nodes = sys.argv[3:] or sorted(p.name for p in collect_dir.iterdir() if p.is_dir())

    recipes = load_recipes(recipes_dir)
    print(f"Loaded {len(recipes)} recipes from {recipes_dir}")
    print(f"  newest stable: "
          f"{max((r for r in recipes if not r.is_ebeta), key=lambda r: r.release_date).name}\n")

    all_ok = True
    for host in nodes:
        d = collect_dir / host
        facts = load_facts(host, d)
        oracle = json.loads(read(d / "oracle-summary.json") or "{}")
        if not oracle:
            print(f"{host}: no oracle output, skipping")
            continue

        scores = get_all_scores(recipes, facts)
        ours = summary(scores)
        diffs = compare_summary(ours, oracle)

        board = "NVIDIA FE" if facts.is_nvidia_fe else f"OEM ({facts.board_vendor})"
        print(f"── {host}  [{board}]  {len(facts.packages)} pkgs, kernel {facts.kernel}")

        name_ok = installed_name(scores) == json.loads(read(d / "oracle-installed-name.json"))
        torn_ok = torn_score(scores) == json.loads(read(d / "oracle-torn-score.json"))
        print(f"   detected OTA   {ours['detected_ota']:<10} "
              f"{'MATCH' if name_ok else 'MISMATCH'}")
        print(f"   torn score     {ours['torn']:<10} "
              f"{'MATCH' if torn_ok else 'MISMATCH'}")
        print(f"   full summary   {len(ours['scores'])} recipes  "
              f"{'IDENTICAL' if not diffs else str(len(diffs)) + ' DIFFERENCES'}")
        for line in diffs[:25]:
            print(f"      ! {line}")
        if len(diffs) > 25:
            print(f"      ... and {len(diffs) - 25} more")
        if diffs or not name_ok or not torn_ok:
            all_ok = False

        # Unprivileged variant: no dmidecode, no mstflint.
        unpriv = NodeFacts(host=host, packages=facts.packages, kernel=facts.kernel,
                           driver=facts.driver, fwupd_devices=facts.fwupd_devices,
                           dmidecode_t45="", connectx_fw="",
                           board_vendor=facts.board_vendor)
        u_scores = get_all_scores(recipes, unpriv, fwupd_only=True)
        u = summary(u_scores)
        verdict = "same" if u["detected_ota"] == ours["detected_ota"] else "DIVERGES"
        print(f"   unprivileged   detected={u['detected_ota']} torn={u['torn']} -> {verdict}")
        print()

    print("ALL NODES IDENTICAL TO ORACLE" if all_ok else "DIFFERENCES FOUND")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
