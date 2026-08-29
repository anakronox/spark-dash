"""The resize corner: what it must not disturb, and what it must not produce.

Same standing as test_section_drag: Svelte 5 runes need a compiler to execute
and this repo has no JS runner, so these are source-level guards on the
invariants whose failure is SILENT. The gesture itself is verified by dragging
in a browser against a running build. Comments are stripped before every check
so no guard can be satisfied by prose.
"""

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "src"
GRIP = FRONTEND / "components" / "CardGrip.svelte"
COLUMN_GRIP = FRONTEND / "components" / "ColumnGrip.svelte"
SECTION = FRONTEND / "components" / "Section.svelte"
SETTINGS = FRONTEND / "components" / "Settings.svelte"
LAYOUT = FRONTEND / "lib" / "layout.svelte.ts"
CHART = FRONTEND / "components" / "MetricChart.svelte"
TRENDS = FRONTEND / "components" / "Trends.svelte"
NETWORK = FRONTEND / "components" / "NetworkTrends.svelte"


def without_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def section_fn(name: str) -> str:
    """One function's body out of Section.svelte, comments stripped."""
    src = without_comments(SECTION.read_text())
    start = src.index(f"function {name}(")
    end = src.index("\n  }", start)
    return src[start:end]


def test_the_grip_stops_its_pointerdown_reaching_the_card():
    """THE FAILURE THIS EXISTS FOR: `Section` starts a card MOVE from a
    pointerdown, and while one is live the band reads the pointer across the
    whole page to pick a drop target. A resize whose pointerdown propagated
    could begin a move, and the card would fly toward a zone while the reader
    believed they were making it taller. Nothing would error."""
    body = without_comments(GRIP.read_text())
    down = body[body.index("function onpointerdown") : body.index("function onkeydown")]
    assert "e.stopPropagation()" in down, "pointerdown propagates to the card underneath"
    assert "e.preventDefault()" in down, "pointerdown is not prevented"


def test_the_grip_stops_its_arrow_and_escape_keys():
    """Section abandons a card move on window Escape, and its handle moves the
    card on ArrowUp/ArrowDown. Both of this grip's bindings collide with one of
    its neighbour's, so resizing by keyboard must not also rearrange the page."""
    body = without_comments(GRIP.read_text())
    keys = body[body.index("function onkeydown") :]
    assert "e.stopPropagation()" in keys, "arrow keys reach the card's own handlers"


def test_the_grip_is_reachable_without_a_mouse():
    """The contract ColumnGrip states: keyboard resizing is not optional, and a
    focusable `separator` is the ARIA pattern for it."""
    src = GRIP.read_text()
    assert 'role="separator"' in src, "not a separator"
    assert 'tabindex="0"' in src, "not focusable"
    body = without_comments(src)
    for key in ("ArrowUp", "ArrowDown", "Home", "Escape"):
        assert key in body, f"{key} is not bound"
    assert "ondblclick" in body, "cannot be reset by double-click"


def test_the_plot_step_matches_the_column_grip():
    """Two resize gestures on one page that stepped differently would be two
    things to learn. 16px, and shift makes it coarse."""
    col = without_comments(COLUMN_GRIP.read_text())
    assert re.search(r"const STEP = 16", col), "ColumnGrip's step moved; this guard is stale"
    step = section_fn("onResizeStep")
    assert "coarse ? 64 : 16" in step, "plot step is not 16px with a x4 coarse"
    assert "coarse" in without_comments(GRIP.read_text()), "shift is not passed through"


def test_a_gesture_can_never_reach_the_uncapped_sentinel():
    """MEASURED BUG, and the reason this guard exists: `0` means UNCAPPED, so
    arithmetic passing through it turned "as small as this card goes" into
    "show me everything". Forty ArrowUps from 12 rows ended on `all rows`, with
    the card twice the size it started."""
    src = without_comments(SECTION.read_text())
    assert "function dragRows" in src, "no guard between the gesture and the sentinel"
    guard = section_fn("dragRows")
    assert "MIN_ROWS" in guard, "dragRows does not floor at MIN_ROWS"
    assert "MAX_ROWS" in guard, "dragRows does not cap at MAX_ROWS"

    for fn in ("onResizeMove", "onResizeStep"):
        body = section_fn(fn)
        rows = [ln for ln in body.splitlines() if "setRows(" in ln]
        assert rows, f"{fn} never sets rows"
        for line in rows:
            assert "dragRows(" in line, f"{fn} sets a row count without dragRows: {line.strip()}"

    lo = int(re.search(r"MIN_ROWS = (\d+)", LAYOUT.read_text()).group(1))
    assert lo >= 1, "MIN_ROWS is itself the sentinel"


def test_the_card_follows_the_pointer_rather_than_leaping():
    """A row cap applies PER TABLE and a plot height per ROW of charts, so the
    card grows by a multiple of what was dragged -- six tables on Temperatures,
    three chart rows on System Activity. Without dividing by that multiple the
    corner runs away from the pointer."""
    body = section_fn("onResizeMove")
    assert "pxPerUnit" in body, "the delta is not scaled to what the card actually grows by"
    measure = section_fn("measure")
    assert "offsetTop" in measure, "chart rows are not counted, so a grid leaps"
    assert "tbody" in measure, "tables are not counted, so a multi-table card leaps"


def test_a_dragged_row_count_survives_a_reload():
    """The cap used to be validated against ROW_CHOICES. The grip drags it
    continuously, so 13 rows is now askable -- and list validation would have
    discarded every dragged value on reload, silently, the card simply back at
    its default."""
    src = without_comments(LAYOUT.read_text())
    reader = src[src.index("function readRows") :][:900]
    assert "ROW_CHOICES.includes" not in reader, "readRows still rejects off-list values"
    assert "clampRows(" in reader, "readRows does not clamp what it loads"


def test_settings_still_shows_a_dragged_value():
    """A <select> whose value matches no option renders BLANK, and would then
    reset the card to the first choice the moment it was touched."""
    src = without_comments(SETTINGS.read_text())
    assert "function rowOptions" in src, "the row select cannot represent an off-list value"
    body = src[src.index("function rowOptions") :][:400]
    assert "ROW_CHOICES.includes(current)" in body, "rowOptions does not check membership"
    assert "rowOptions(" in src[src.index("<select") :], "the select does not use rowOptions"


def test_a_stored_plot_height_is_clamped_on_the_way_in():
    """A hand-edited or stale value is the same hazard readRows guards: a
    4000px plot would push every control that could undo it off the page."""
    src = without_comments(LAYOUT.read_text())
    reader = src[src.index("function readPlotHeights") : src.index("function clampPlot")]
    assert "clampPlot(" in reader, "readPlotHeights does not clamp what it loads"

    clamp = src[src.index("function clampPlot") :][:300]
    assert "MAX_PLOT_PX" in clamp and "MIN_PLOT_PX" in clamp, "clampPlot ignores its bounds"

    setter = src[src.index("setPlotHeight(") :][:300]
    assert "clampPlot(" in setter, "setPlotHeight accepts an unbounded drag"


def test_the_bounds_leave_a_readable_card_at_either_end():
    src = LAYOUT.read_text()
    lo = int(re.search(r"MIN_PLOT_PX = (\d+)", src).group(1))
    hi = int(re.search(r"MAX_PLOT_PX = (\d+)", src).group(1))
    default = int(re.search(r"DEFAULT_PLOT_PX = (\d+)", src).group(1))
    assert lo >= 60, "a plot this short is a smear, not a line"
    assert hi <= 600, "one plot taller than a viewport stops being a small multiple"
    assert lo <= default <= hi, f"default {default} is outside [{lo}, {hi}]"


def test_the_default_plot_height_is_stated_once():
    """MetricChart's prop default and the store's default must not be two
    literals that happen to agree: charts would render at one height while the
    grip reported another, and nothing would error."""
    chart = without_comments(CHART.read_text())
    assert "height = DEFAULT_PLOT_PX" in chart, "MetricChart repeats the default height"
    for card, name in ((TRENDS, "Trends"), (NETWORK, "NetworkTrends")):
        src = without_comments(card.read_text())
        assert "plotHeight = DEFAULT_PLOT_PX" in src, f"{name} repeats the default height"


def test_reset_puts_the_heights_the_caps_and_the_band_mode_back():
    """The rule layout.reset() already states for hidden sections and switched
    off columns: one control has to put everything back."""
    src = without_comments(LAYOUT.read_text())
    body = src[src.index("  reset() {") :]
    body = body[: body.index("\n  }")]
    assert "this.plotHeights = {}" in body, "reset leaves dragged plot heights in place"
    assert "this.rows = {}" in body, "reset leaves dragged row caps in place"
    assert "PLOT_HEIGHT_KEY" in body, "reset leaves the stored heights on disk"
    assert "setBandMode('aligned')" in body, "reset leaves the page in packed mode"


def test_every_card_has_exactly_one_resize_corner():
    """One grip per CARD -- charts that share an x axis and not a height stop
    being small multiples, and a table capped per-table needs one answer for
    the card, not one per table. It lives in the frame, so every card gets it
    from one place."""
    src = without_comments(SECTION.read_text())
    assert src.count("<CardGrip") == 1, "Section renders more than one resize corner"
    for card, name in ((TRENDS, "Trends"), (NETWORK, "NetworkTrends")):
        body = without_comments(card.read_text())
        assert "CardGrip" not in body and "RowGrip" not in body, (
            f"{name} carries its own resize control as well as the card frame's"
        )


def test_a_collapsed_card_has_no_resize_corner():
    """A collapsed card is a 40px stub. A resize corner on it would offer a
    gesture with nothing to act on."""
    src = without_comments(SECTION.read_text())
    branch = src[src.index("{#if collapsed}") : src.index("</div>", src.index("{#if collapsed}"))]
    stub, rest = branch.split("{:else}", 1)
    assert "<CardGrip" not in stub, "the collapsed stub renders a resize corner"
    assert "<CardGrip" in rest, "the expanded card does not render one"
