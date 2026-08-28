"""Per-card plot height: the grip, its bounds, and what it must not disturb.

Same standing as test_section_drag: Svelte 5 runes need a compiler to execute
and this repo has no JS runner, so these are source-level guards on the
invariants whose failure is SILENT. The gesture itself is verified by dragging
in a browser against a running build. Comments are stripped before every check
so no guard can be satisfied by prose.
"""

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "src"
GRIP = FRONTEND / "components" / "RowGrip.svelte"
COLUMN_GRIP = FRONTEND / "components" / "ColumnGrip.svelte"
LAYOUT = FRONTEND / "lib" / "layout.svelte.ts"
CHART = FRONTEND / "components" / "MetricChart.svelte"
TRENDS = FRONTEND / "components" / "Trends.svelte"
NETWORK = FRONTEND / "components" / "NetworkTrends.svelte"


def without_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def test_the_grip_stops_its_pointerdown_reaching_the_card():
    """THE FAILURE THIS EXISTS FOR: `Section` starts a card MOVE from a
    pointerdown, and while one is live the band reads the pointer across the
    whole page to pick a drop target. A resize whose pointerdown propagated
    could begin a move, and the card would fly toward a zone while the reader
    believed they were making a chart taller. Nothing would error."""
    src = without_comments(GRIP.read_text())
    body = src[src.index("function onpointerdown") : src.index("function onkeydown")]
    assert "e.stopPropagation()" in body, "pointerdown propagates to the card underneath"
    assert "e.preventDefault()" in body, "pointerdown is not prevented"


def test_the_grip_stops_its_arrow_and_escape_keys():
    """Section listens for Escape on the WINDOW to abandon a move, and its
    handle moves the card on ArrowUp/ArrowDown. Resizing with the keyboard
    must not also reorder the page."""
    src = without_comments(GRIP.read_text())
    body = src[src.index("function onkeydown") :]
    assert "e.stopPropagation()" in body, "arrow keys reach the card's own handlers"


def test_the_grip_is_reachable_without_a_mouse():
    """The contract ColumnGrip states: keyboard resizing is not optional, and
    a focusable `separator` is the ARIA pattern for it."""
    src = GRIP.read_text()
    assert 'role="separator"' in src, "not a separator"
    assert 'tabindex="0"' in src, "not focusable"
    body = without_comments(src)
    for key in ("ArrowUp", "ArrowDown"):
        assert key in body, f"{key} does not resize"
    assert "Escape" in body and "Home" in body, "no keyboard route back to the default"


def test_both_grips_share_one_step_and_one_escape():
    """Two resize gestures on one page that behaved differently would be two
    things to learn. The step size and the reset keys must match."""
    row = without_comments(GRIP.read_text())
    col = without_comments(COLUMN_GRIP.read_text())
    steps = {
        name: re.search(r"const STEP = (\d+)", src).group(1)
        for name, src in (("RowGrip", row), ("ColumnGrip", col))
    }
    assert steps["RowGrip"] == steps["ColumnGrip"], f"step sizes diverged: {steps}"
    for src, name in ((row, "RowGrip"), (col, "ColumnGrip")):
        assert "e.shiftKey" in src, f"{name} has no coarse step"
        assert "ondblclick" in src, f"{name} cannot be reset by double-click"


def test_a_stored_height_is_clamped_on_the_way_in():
    """A hand-edited or stale localStorage value is the same hazard readRows
    guards. A 4000px plot would push every other card off the page, and the
    only control that could undo it would be off the page with them."""
    src = without_comments(LAYOUT.read_text())
    reader = src[src.index("function readPlotHeights") : src.index("function clampPlot")]
    assert "clampPlot(" in reader, "readPlotHeights does not clamp what it loads"

    clamp = src[src.index("function clampPlot") :][:300]
    assert "MAX_PLOT_PX" in clamp and "MIN_PLOT_PX" in clamp, "clampPlot ignores its bounds"

    setter = src[src.index("setPlotHeight(") :][:300]
    assert "clampPlot(" in setter, "setPlotHeight accepts an unbounded drag"


def test_the_bounds_leave_a_readable_chart_at_either_end():
    src = LAYOUT.read_text()
    lo = int(re.search(r"MIN_PLOT_PX = (\d+)", src).group(1))
    hi = int(re.search(r"MAX_PLOT_PX = (\d+)", src).group(1))
    default = int(re.search(r"DEFAULT_PLOT_PX = (\d+)", src).group(1))
    assert lo >= 60, "a plot this short is a smear, not a line"
    assert hi <= 600, "one plot taller than a viewport stops being a small multiple"
    assert lo <= default <= hi, f"default {default} is outside [{lo}, {hi}]"


def test_the_default_height_is_stated_once():
    """MetricChart's prop default and the store's default must not be two
    literals that happen to agree: charts would render at one height while the
    grip reported another, and nothing would error."""
    chart = without_comments(CHART.read_text())
    assert "height = DEFAULT_PLOT_PX" in chart, "MetricChart repeats the default height"
    assert re.search(r"import \{[^}]*DEFAULT_PLOT_PX[^}]*\} from '../lib/layout.svelte'", chart), (
        "MetricChart does not import the shared default"
    )
    for card, name in ((TRENDS, "Trends"), (NETWORK, "NetworkTrends")):
        src = without_comments(card.read_text())
        assert "plotHeight = DEFAULT_PLOT_PX" in src, f"{name} repeats the default height"


def test_reset_puts_the_heights_and_the_band_mode_back():
    """The rule layout.reset() already states for hidden sections and switched
    off columns: one control has to put everything back. A card dragged to the
    floor and a page left in packed mode are both states the reader may not
    remember making."""
    src = without_comments(LAYOUT.read_text())
    body = src[src.index("  reset() {") :]
    body = body[: body.index("\n  }")]
    assert "this.plotHeights = {}" in body, "reset leaves dragged plot heights in place"
    assert "PLOT_HEIGHT_KEY" in body, "reset leaves the stored heights on disk"
    assert "setBandMode('aligned')" in body, "reset leaves the page in packed mode"


def test_every_plot_on_a_card_shares_one_height():
    """Charts that share an x axis and not a height stop being small multiples
    -- the whole reason they are a grid. One grip per card, not one per plot."""
    for card, name in ((TRENDS, "Trends"), (NETWORK, "NetworkTrends")):
        src = without_comments(card.read_text())
        assert src.count("<RowGrip") == 1, f"{name} has more than one height control"
        charts = src.count("<MetricChart")
        assert charts >= 1, f"{name} draws no charts"
        assert src.count("height={plotHeight}") == charts, (
            f"{name} has {charts} charts but not all take the card's height"
        )
