"""Run the client-stats wording for real, under node (roadmap AM5).

The same arrangement as test_fleet_js.py: `lib/clients.ts` is plain
TypeScript that decides which of Settings' five branches renders and what it
says, and the ORDER of those checks is the point. Skipped, not failed, when
node or esbuild is absent.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DRIVER = ROOT / "tests" / "js" / "clients.test.mjs"
ESBUILD = ROOT / "frontend" / "node_modules" / ".bin" / "esbuild"


def test_the_driver_exists():
    assert DRIVER.is_file(), f"missing {DRIVER.relative_to(ROOT)}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.skipif(not ESBUILD.is_file(), reason="frontend deps are not installed")
def test_clients_wording(tmp_path: Path):
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
