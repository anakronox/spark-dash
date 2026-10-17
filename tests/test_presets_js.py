"""Layout presets (roadmap BK), run under node against an in-memory
localStorage. Same toolchain and skip rules as test_gauges_js.py."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DRIVER = ROOT / "tests" / "js" / "presets.test.mjs"
ESBUILD = ROOT / "frontend" / "node_modules" / ".bin" / "esbuild"
LAYOUT = ROOT / "frontend" / "src" / "lib" / "layout.svelte.ts"


@pytest.fixture(scope="module")
def out(tmp_path_factory) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    if not ESBUILD.is_file():
        pytest.skip("frontend deps are not installed")
    bundle = tmp_path_factory.mktemp("presets") / "bundle.mjs"
    build = subprocess.run(
        [str(ESBUILD), str(DRIVER), "--bundle", "--format=esm", "--platform=node",
         "--packages=external", f"--outfile={bundle}"],
        capture_output=True, text=True,
    )
    assert build.returncode == 0, f"esbuild failed:\n{build.stderr}"
    run = subprocess.run(["node", str(bundle)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def section_ids() -> set[str]:
    src = LAYOUT.read_text()
    block = src[src.index("export const SECTIONS"):src.index("const DEFAULT_ORDER")]
    return set(re.findall(r"\{ id: '([^']+)'", block))


def test_three_presets_and_the_default_is_side_by_side(out):
    assert [p["id"] for p in out["presets"]] == ["side-by-side", "stacked", "wall"]
    assert out["default"] == "side-by-side"
    assert out["presets"][1]["values"] == {}, "Stacked is the page with nothing saved"


def test_every_preset_names_real_sections_and_zones(out):
    known = section_ids()
    for p in out["presets"]:
        v = p["values"]
        assert all(k.startswith("spark-dash.") for k in v)
        order = json.loads(v.get("spark-dash.section-order.v1", "[]"))
        if order:
            assert set(order) == known, f"{p['id']} order must list every section once"
        placement = json.loads(v.get("spark-dash.section-placement.v1", "{}"))
        assert set(placement) <= known
        assert set(placement.values()) <= {"full", "left", "right", "nodes"}
        assert set(json.loads(v.get("spark-dash.section-hidden.v1", "[]"))) <= known


def test_a_first_visit_gets_the_default_and_nobody_else_does(out):
    assert out["seeded"] == [True, True, False, False]
    assert out["fresh"]["spark-dash.layout-preset.v1"] == "side-by-side"
    assert out["fresh"]["spark-dash.compact-cards.v1"] == "1"
    assert out["freshThemed"]["spark-dash.theme.v1"] == "paper", "a theme alone is still a first visit"
    assert out["freshThemed"]["spark-dash.page-title.v1"] == "lab", "so is a page title"
    assert out["arranged"] == {"spark-dash.section-order.v1": '["models"]'}


def test_a_preset_replaces_the_layout_and_keeps_the_rest(out):
    s = out["switched"]
    assert s["spark-dash.theme.v1"] == "cyberpunk" and s["spark-dash.page-title.v1"] == "lab"
    assert s["spark-dash.onboarding-seen.v1"] == "1" and s["spark-dash.tour-done.v1"] == "1"
    assert s["other-site.key"] == "x", "only this page's keys are touched"
    assert "spark-dash.trend-view.v1:history#2" not in s, "a copy's view goes with the layout"
    assert "spark-dash.layout-baseline.v1" not in s, "the next load measures the new baseline"
    assert s["spark-dash.layout-preset.v1"] == "wall"
    assert s["spark-dash.trend-view.v1"] == "gauges"
    assert out["current"] == ["wall", None, None]


def test_the_page_title_reaches_the_header_and_the_tab():
    """BL: the reader's name for the page, kept in this browser."""
    src = (ROOT / "frontend" / "src")
    store = (src / "lib" / "title.svelte.ts").read_text()
    assert "DEFAULT_TITLE = 'spark-dash'" in store
    app = (src / "App.svelte").read_text()
    assert "{pageTitle.custom}</h1>" in app and "document.title = pageTitle.text" in app
    assert "pageTitle.set(" in (src / "components" / "Settings.svelte").read_text()
