"""Run the chart grid geometry and time labels under node.

Same toolchain and the same skip rules as test_network_history.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DRIVER = ROOT / "tests" / "js" / "chart_grid.test.mjs"
ESBUILD = ROOT / "frontend" / "node_modules" / ".bin" / "esbuild"


def test_the_driver_exists():
    """Guards the skip below: if the file is renamed, the rest of this module
    would skip silently and look like it had simply not been installed for."""
    assert DRIVER.is_file(), f"missing {DRIVER.relative_to(ROOT)}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.skipif(not ESBUILD.is_file(), reason="frontend deps are not installed")
def test_chart_grid(tmp_path: Path):
    bundle = tmp_path / "bundle.mjs"
    build = subprocess.run(
        [
            str(ESBUILD),
            str(DRIVER),
            "--bundle",
            "--format=esm",
            "--platform=node",
            # node's own modules stay external, or esbuild tries to bundle
            # `node:assert` into the output and fails.
            "--packages=external",
            f"--outfile={bundle}",
        ],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, f"esbuild failed:\n{build.stderr}"

    # UTC, so "local time" in the labels is the same on every machine.
    run = subprocess.run(
        ["node", str(bundle)], capture_output=True, text=True, env={**os.environ, "TZ": "UTC"}
    )
    # stderr carries the per-case failures, stdout the tally. Both are printed
    # on failure because "3/16 passed" alone does not say which three.
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    assert "passed" in run.stdout, run.stdout
