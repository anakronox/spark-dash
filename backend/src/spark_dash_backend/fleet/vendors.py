"""Who built the board, which platform-firmware bundle it is on, and what that
vendor's newest published bundle is.

WHY THIS EXISTS. NVIDIA's release recipes name firmware versions (SOCFW 2.155.11,
EC 3.5.8 for July 2026) that only NVIDIA-built boards receive through the
release itself. A partner board — ASUS GX10, and in time Dell, HP, Lenovo and
the rest — gets its platform firmware from that vendor as a signed bundle, on
the vendor's schedule. So on a partner board "July 2026 available" can be true
with nothing whatsoever to install, and the honest thing to say is not
"outdated" but "as current as ASUS has published; NVIDIA's newer firmware is
pending from the vendor". Telling those two apart needs to know what the
vendor's newest bundle is, and that is what the table below records.

HAND-MAINTAINED, AND SAYS SO. There is no API for a vendor's download page.
Each entry carries the date it was last checked, and the UI shows it, so a
stale table reads as stale rather than as fact.

THE VENDOR IS THE BOARD VENDOR, NOT A MODEL STRING. NVIDIA's own checker branches
on `/sys/class/dmi/id/board_vendor` containing NVIDIA; everything else takes the
partner path. Matching here does the same, with the product name to pick the
bundle regex, so a partner board not in the table still gets the partner
treatment — just without a "vendor's newest" to compare against.

COMPARE IN NVIDIA'S NUMBERING, NEVER THE VENDOR'S. ASUS lists its 0105 bundle as
"SOC FW 3.0.7 / EC 3.3.2.4"; the board reports the same firmware through
`dmidecode -t 45` as FLASH 2.152.15 / EC Firmware 3.3.2, which is what the recipes
use. The vendor's component list is kept here for the human reading the page;
the scorer's found-versus-expected is the comparison.
"""

from __future__ import annotations

import re

VENDORS: list[dict] = [
    {
        "id": "asus-gx10",
        "vendor": "ASUS",
        "model": "Ascent GX10",
        # substring of board_vendor, case-insensitive; prefix of product_name
        "board_vendor": "ASUSTEK",
        "product": "GX10",
        # the bundle number inside the BIOS version string, e.g. GX10DGX.0105.2026.0505.1153
        "bundle_from_bios": r"GX10DGX\.(\d{4})",
        "newest": {
            "bundle": "0105",
            "published": "2026-08-11",
            "components": {"SOC FW": "3.0.7", "BIOS": "0105", "EC": "3.3.2.4",
                           "TPM": "7.2.4.1", "PD": "5.22"},
            # what it reads as through dmidecode -t 45, i.e. in the recipes' numbering
            "reads_as": {"SOCFW": "2.152.15", "EC": "3.3.2"},
            # the NVIDIA release whose firmware baseline this bundle satisfies
            "satisfies": "OTA2604",
            "url": ("https://www.asus.com/networking-iot-servers/desktop-ai-supercomputer/"
                    "ultra-small-ai-supercomputers/asus-ascent-gx10/helpdesk_bios?model2Name=ASUS-Ascent-GX10"),
            "checked": "2026-09-07",
        },
    },
]


def _bundle_int(s: str | None) -> int | None:
    try:
        return int(s) if s is not None else None
    except ValueError:
        return None


def platform_firmware(board_vendor: str, product: str, bios_version: str, bios_date: str) -> dict:
    """The platform-firmware line for one board.

    `state` is one of:
      nvidia-release  NVIDIA-built board; firmware arrives with the NVIDIA release
      current         partner board on the vendor's newest published bundle
      behind          partner board; the vendor has published a newer bundle
      newer           partner board reports a bundle newer than the table knows (table is stale)
      unknown         partner board with no table entry, or an unparseable BIOS string
    """
    bv = (board_vendor or "").strip()
    base = {"board_vendor": bv, "product": (product or "").strip(),
            "bios_version": (bios_version or "").strip(), "bios_date": (bios_date or "").strip()}

    if "NVIDIA" in bv.upper():
        return {**base, "vendor": "NVIDIA", "model": "DGX Spark Founders Edition",
                "installed": None, "newest": None, "state": "nvidia-release"}

    for v in VENDORS:
        if v["board_vendor"] not in bv.upper():
            continue
        # a fact file from before the collector gathered product_name has none;
        # the vendor still matches, the bundle simply reads as unknown until the
        # next check fills the BIOS string in
        if v.get("product") and base["product"] and \
                not base["product"].upper().startswith(v["product"].upper()):
            continue
        m = re.search(v["bundle_from_bios"], base["bios_version"])
        installed = m.group(1) if m else None
        newest = v["newest"]
        have, want = _bundle_int(installed), _bundle_int(newest["bundle"])
        if have is None or want is None:
            state = "unknown"
        elif have == want:
            state = "current"
        elif have < want:
            state = "behind"
        else:
            state = "newer"
        return {**base, "vendor": v["vendor"], "model": v["model"], "installed": installed,
                "newest": newest, "state": state}

    vendor = bv.split()[0].title() if bv else "unknown"
    return {**base, "vendor": vendor, "model": base["product"], "installed": None,
            "newest": None, "state": "unknown"}
