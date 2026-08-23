"""Run the Network history grouping logic for real, under node.

THE FIRST EXECUTED FRONTEND TEST IN THIS REPO, and the reason is specific to
what it covers. Every other frontend guard here is a regex over the source,
because Svelte runes need a compiler to run and there is no JS test runner. That
is a fair trade for a drag gesture: its behaviour lives in the DOM, so a source
guard and a real drag in a browser between them cover it.

`network-history.ts` is not that. It is plain TypeScript, no runes and no DOM,
and it is almost entirely branching — which interfaces are quiet, what order
they come in, whether a fault chart exists at all. A regex asserting the word
"quiet" appears somewhere would pass on an implementation that has every one of
those backwards.

esbuild comes with Vite and is already in frontend/node_modules, so this needs
no new dependency. It is SKIPPED rather than failed when node or esbuild is
absent: a checkout that has not run `npm install` should still get a green
suite, and this must not be the test that makes `pytest` require a JS toolchain.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DRIVER = ROOT / "tests" / "js" / "network_history.test.mjs"
ESBUILD = ROOT / "frontend" / "node_modules" / ".bin" / "esbuild"


def test_the_driver_exists():
    """Guards the skip below: if the file is renamed, the rest of this module
    would skip silently and look like it had simply not been installed for."""
    assert DRIVER.is_file(), f"missing {DRIVER.relative_to(ROOT)}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.skipif(not ESBUILD.is_file(), reason="frontend deps are not installed")
def test_network_history_grouping(tmp_path: Path):
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

    run = subprocess.run(["node", str(bundle)], capture_output=True, text=True)
    # stderr carries the per-case failures, stdout the tally. Both are printed
    # on failure because "3/16 passed" alone does not say which three.
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    assert "passed" in run.stdout, run.stdout
