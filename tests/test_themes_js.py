"""Theme families and modes (roadmap BM), run under node. Same toolchain and
skip rules as test_gauges_js.py."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DRIVER = ROOT / "tests" / "js" / "themes.test.mjs"
ESBUILD = ROOT / "frontend" / "node_modules" / ".bin" / "esbuild"
APP_CSS = ROOT / "frontend" / "src" / "app.css"


@pytest.fixture(scope="module")
def out(tmp_path_factory) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    if not ESBUILD.is_file():
        pytest.skip("frontend deps are not installed")
    bundle = tmp_path_factory.mktemp("themes") / "bundle.mjs"
    build = subprocess.run(
        [str(ESBUILD), str(DRIVER), "--bundle", "--format=esm", "--platform=node",
         "--packages=external", f"--outfile={bundle}"],
        capture_output=True, text=True,
    )
    assert build.returncode == 0, f"esbuild failed:\n{build.stderr}"
    run = subprocess.run(["node", str(bundle)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def css_modes() -> dict[str, str]:
    text = APP_CSS.read_text()
    out = {"dark": "dark"}
    for name, body in re.findall(r":root\[data-theme='([a-z-]+)'\]\s*\{(.*?)\n\}", text, re.S):
        out[name] = re.search(r"color-scheme:\s*(light|dark)", body).group(1)
    return out


def test_every_family_has_a_dark_and_a_light_palette(out):
    modes = css_modes()
    for f in out["families"]:
        assert modes[f["dark"]] == "dark", f"{f['id']}: {f['dark']} is not a dark palette"
        assert modes[f["light"]] == "light", f"{f['id']}: {f['light']} is not a light palette"
    used = {p for f in out["families"] for p in (f["dark"], f["light"])}
    assert used == set(modes), "a palette no family reaches, or a family with no palette"


def test_the_requested_families_are_there(out):
    labels = [f["label"] for f in out["families"]]
    for want in ("Catppuccin", "Solarized", "Darcula"):
        assert want in labels
    assert out["modes"] == ["auto", "dark", "light"]


def test_a_stored_theme_from_before_keeps_looking_the_same(out):
    m = out["migrated"]
    assert m["auto"] == {"family": "default", "mode": "auto"}
    assert m["dark"] == {"family": "default", "mode": "dark"}
    assert m["light"] == {"family": "default", "mode": "light"}
    assert m["paper"] == {"family": "paper", "mode": "light"}, "paper was a light theme"
    assert m["cyberpunk"] == {"family": "cyberpunk", "mode": "dark"}
    assert m["nvidia"] == {"family": "forest", "mode": "dark"}
    assert m["gone"] == m["null"] == {"family": "default", "mode": "dark"}
    assert out["unknown"] == ["dark", "light"]
