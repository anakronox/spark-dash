"""The three lines a Spark's row is built from, checked against the captured
inputs from a real mixed fleet: one NVIDIA-built board and two ASUS GX10s.

The fixtures under fleet-collector-inputs/ predate the BIOS fields the
collector now gathers, so those are supplied here with the values read from
the boards on 2026-09-07. They came across with the code in AL3a; upstream
they live at reference/collector-inputs/."""

from __future__ import annotations

import unittest
from pathlib import Path

from spark_dash_backend.fleet import posture

HERE = Path(__file__).resolve().parent
INPUTS = HERE / "fleet-collector-inputs"
RECIPES = Path(posture.__file__).resolve().parent / "recipes"

BIOS = {
    "sparky":   ("GX10", "GX10DGX.0105.2026.0505.1153", "05/05/2026"),
    "sparkjr":  ("GX10", "GX10DGX.0105.2026.0505.1153", "05/05/2026"),
    "sparketa": ("NVIDIA_DGX_Spark", "5.36_0ACUM027", "08/06/2025"),
}


def facts_for(node: str) -> dict:
    d = INPUTS / node
    read = lambda name: (d / name).read_text() if (d / name).exists() else ""
    driver = read("driver.txt").split(":")[-1].strip()
    product, bios, date = BIOS[node]
    return {"packages": read("dpkg-query.txt"), "kernel": read("uname-r.txt").strip(), "driver": driver,
            "fwupd_devices": read("fwupd-devices.json"), "dmidecode_t45": read("dmidecode-45.txt"),
            "mstflint": read("mstflint.txt"), "board_vendor": read("board_vendor.txt").strip(),
            "product_name": product, "bios_version": bios, "bios_date": date,
            "apt_check": "0;0", "apt_sim": "", "fwupd_updates": ""}


def build(node: str) -> dict:
    return posture.build({"name": node, "host": node}, facts_for(node), RECIPES)


@unittest.skipUnless(INPUTS.exists(), "fixtures not present")
class PartnerBoards(unittest.TestCase):
    """ASUS GX10s, judged on what fwupd offers them (roadmap AV)."""

    def test_partner_firmware_comes_from_fwupd_alone(self):
        for node in ("sparky", "sparkjr"):
            p = build(node)
            pf = p["platform_firmware"]
            with self.subTest(node=node):
                self.assertEqual(pf["vendor"], "Asus")
                self.assertEqual(pf["state"], "current", "fwupd offered nothing in these captures")
                self.assertEqual(p["firmware_gap"]["state"], "current")
                self.assertEqual(p["firmware_gap"]["outstanding"], [])
                self.assertIn("Embedded Controller", {d["name"] for d in pf["devices"]})
                self.assertNotIn("newest", pf, "no vendor table: nothing from outside the board")

    def test_nvidia_targets_are_not_listed_against_a_partner_board(self):
        """ASUS numbers its firmware differently; NVIDIA's 2.155.11 against
        the GX10's 2.152.15 was a gap that never closed."""
        for node in ("sparky", "sparkjr"):
            with self.subTest(node=node):
                self.assertNotIn("firmware", {b["kind"] for b in build(node)["release"]["behind"]})

    def test_a_partner_board_is_on_the_release_its_software_matches(self):
        for node in ("sparky", "sparkjr"):
            p = build(node)
            with self.subTest(node=node):
                self.assertEqual(p["release"]["on"], p["software"]["on"])

    def test_firmware_fwupd_offers_is_installable(self):
        offer = (
            '{"Devices": [{"Name": "UEFI Device Firmware", "Vendor": "Asus", "Version": "0x03000007",'
            ' "Flags": ["updatable", "needs-reboot"], "Releases": [{"Version": "0x03000008", "Size": 30411545}]}]}'
        )
        p = posture.build({"name": "sparky", "host": "sparky"}, {**facts_for("sparky"), "fwupd_updates": offer}, RECIPES)
        self.assertEqual(p["platform_firmware"]["state"], "installable")
        self.assertEqual(p["firmware_gap"]["state"], "installable")
        self.assertEqual(p["firmware_updates"][0]["after"], "3.0.8")

    def test_nvidia_board_keeps_nvidias_release_check(self):
        p = build("sparketa")
        self.assertEqual(p["platform_firmware"]["vendor"], "NVIDIA")
        self.assertEqual(p["platform_firmware"]["state"], "nvidia-release")
        self.assertIn(p["firmware_gap"]["state"], ("current", "installable", "pending-nvidia"))

    def test_software_line_is_judged_without_firmware(self):
        for node in ("sparky", "sparkjr", "sparketa"):
            sw = build(node)["software"]
            with self.subTest(node=node):
                self.assertIn(sw["state"], ("current", "installable", "held"))
                for f in sw["behind"]:
                    self.assertIsNotNone(f["installed"], "behind means installed but old")
                for f in sw["missing"]:
                    self.assertIsNone(f["installed"], "missing means not installed at all")
                self.assertEqual(len(sw["failing"]), len(sw["behind"]) + len(sw["missing"]))

    def test_never_installed_packages_do_not_hold_a_release_back(self):
        """sparky's two station packages were removed on purpose; they must not
        drag the software line down to the last release that did not list them."""
        sw = build("sparky")["software"]
        missing = {f["name"] for f in sw["missing"]}
        if missing:
            self.assertNotEqual(sw["on"], "OTA1.1")


class HeldPackages(unittest.TestCase):
    """AL6.3: a held package is invisible in the plan, so the record has to
    carry it separately or "nothing to install" reads as "up to date".

    The sample is the real `apt-get -s full-upgrade` from sparketa on
    2026-09-22, the day six kernel metapackages were held there."""

    SIM = """Reading package lists...
Building dependency tree...
The following packages have been kept back:
  libnvidia-cfg1-580 libnvidia-compute-580 linux-image-nvidia-hwe-24.04
  linux-nvidia-hwe-24.04
The following packages will be upgraded:
  curl
Inst curl [8.5.0-2ubuntu10.6] (8.5.0-2ubuntu10.7 Ubuntu:24.04/noble-security [arm64])
Conf curl (8.5.0-2ubuntu10.7 Ubuntu:24.04/noble-security [arm64])
1 upgraded, 0 newly installed, 0 to remove and 4 not upgraded.
"""

    def test_kept_back_is_parsed_and_is_not_in_the_plan(self):
        r = posture.parse_apt_sim(self.SIM)
        self.assertEqual([p["name"] for p in r["packages"]], ["curl"])
        self.assertEqual(r["kept_back"], ["libnvidia-cfg1-580", "libnvidia-compute-580",
                                          "linux-image-nvidia-hwe-24.04", "linux-nvidia-hwe-24.04"])
        # the point of the whole item: the held kernel is NOT in the plan
        self.assertNotIn("linux-nvidia-hwe-24.04", [p["name"] for p in r["packages"]])

    def test_no_kept_back_section_is_an_empty_list_not_a_missing_key(self):
        r = posture.parse_apt_sim("1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n")
        self.assertEqual(r["kept_back"], [])

    def test_holds_and_kept_back_reach_the_record_separately(self):
        facts = facts_for("sparketa")
        facts["apt_sim"] = self.SIM
        facts["apt_holds"] = ["linux-nvidia-hwe-24.04"]
        rec = posture.build({"name": "sparketa", "host": "h"}, facts, RECIPES)
        u = rec["updates"]
        # what a person pinned, and what apt declined because of it: not the
        # same list, and the second is the longer one
        self.assertEqual(u["held"], ["linux-nvidia-hwe-24.04"])
        self.assertIn("libnvidia-compute-580", u["kept_back"])
        self.assertNotIn("libnvidia-compute-580", u["held"])

    def test_a_node_with_no_holds_says_so_with_an_empty_list(self):
        facts = facts_for("sparky")
        rec = posture.build({"name": "sparky", "host": "h"}, facts, RECIPES)
        self.assertEqual(rec["updates"]["held"], [])


if __name__ == "__main__":
    unittest.main()
