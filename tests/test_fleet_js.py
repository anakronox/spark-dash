"""Run the fleet updater's status wording for real, under node (roadmap AK).

The same arrangement as test_network_history.py, for the same reason:
`lib/fleet.ts` is plain TypeScript with no runes and no DOM, and it is almost
entirely branching -- ten pill states, four sentences for "nothing to
install", a progress bar with a band per step. It is also a PORT: the fleet
page's own functions, which a source guard could not check against anything.
Skipped, not failed, when node or esbuild is absent.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DRIVER = ROOT / "tests" / "js" / "fleet.test.mjs"
ESBUILD = ROOT / "frontend" / "node_modules" / ".bin" / "esbuild"


def test_the_driver_exists():
    assert DRIVER.is_file(), f"missing {DRIVER.relative_to(ROOT)}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.skipif(not ESBUILD.is_file(), reason="frontend deps are not installed")
def test_fleet_status_wording(tmp_path: Path):
    bundle = tmp_path / "bundle.mjs"
    build = subprocess.run(
        [
            str(ESBUILD),
            str(DRIVER),
            "--bundle",
            "--format=esm",
            "--platform=node",
            "--packages=external",
            f"--outfile={bundle}",
        ],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, f"esbuild failed:\n{build.stderr}"

    run = subprocess.run(["node", str(bundle)], capture_output=True, text=True)
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    assert "passed" in run.stdout, run.stdout
