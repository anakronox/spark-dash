"""Settings' llama.cpp router keys: the id the backend uses and the key
check made before sending, run under node.

Same toolchain and skip rules as test_gauges_js.py.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DRIVER = ROOT / "tests" / "js" / "router_keys.test.mjs"
ESBUILD = ROOT / "frontend" / "node_modules" / ".bin" / "esbuild"


def test_the_driver_exists():
    assert DRIVER.is_file(), f"missing {DRIVER.relative_to(ROOT)}"


@pytest.fixture(scope="module")
def out(tmp_path_factory) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    if not ESBUILD.is_file():
        pytest.skip("frontend deps are not installed")
    bundle = tmp_path_factory.mktemp("router_keys") / "bundle.mjs"
    build = subprocess.run(
        [str(ESBUILD), str(DRIVER), "--bundle", "--format=esm", "--platform=node",
         "--packages=external", f"--outfile={bundle}"],
        capture_output=True, text=True,
    )
    assert build.returncode == 0, f"esbuild failed:\n{build.stderr}"
    run = subprocess.run(["node", str(bundle)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_the_id_is_node_and_port(out):
    """As the backend's router_keys.key_id names it."""
    assert out["id"] == "sparky:8108"


def test_the_check_matches_the_backends(out):
    assert out["ok"] is None
    # No minimum: a router's key is whatever its operator chose.
    assert out["short"] is None
    for bad in ("empty", "long", "space", "unicode"):
        assert out[bad], bad
