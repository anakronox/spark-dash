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
APP_CSS = FRONTEND / "app.css"
APP = FRONTEND / "App.svelte"
CHART = FRONTEND / "components" / "MetricChart.svelte"
TRENDS = FRONTEND / "components" / "Trends.svelte"
NETWORK = FRONTEND / "components" / "NetworkTrends.svelte"


def without_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def css_block(path: Path, selector: str) -> str:
    """One CSS rule's body, COMMENTS STRIPPED.

    Reading the raw file is a trap this file's own header warns about, and it
    caught me: the comment above `min-height` explains why `max(0px, ...)` is
    needed, so it contains the string "min-height" and a guard reading the raw
    text passed against a rule that had been deleted.
    """
    src = without_comments(path.read_text())
    start = src.index(selector)
    return src[start : src.index("}", start)]


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


def test_every_card_resizes_in_whole_table_rows():
    """THE UNIT IS THE POINT. Cards line up across the columns because each one
    spans a whole number of table rows, and that only survives if the gesture
    moves in whole rows too. A table already did, because a row count is an
    integer; a chart card slid continuously and could stop at any pixel."""
    move = section_fn("onResizeMove")
    assert re.search(r"Math\.round\(dy / rowUnit\(\)\)", move), (
        "pointer travel is not snapped to whole rows before it is applied"
    )

    apply = section_fn("applyResize")
    assert "modules * unit" in apply.replace("(", "").replace(")", ""), (
        "the module count is not converted back into the card's own units"
    )

    step = section_fn("onResizeStep")
    assert "applyResize(" in step, "the keyboard does not go through the same path as the drag"
    assert "coarse" in step, "no coarse step"
    assert "coarse" in without_comments(GRIP.read_text()), "shift is not passed through"


def test_the_grip_keeps_the_column_grip_s_shift_contract():
    """Two resize gestures on one page that behaved differently would be two
    things to learn. The units differ -- pixels of column, rows of card -- but
    shift makes both coarse and both reset the same way."""
    col = without_comments(COLUMN_GRIP.read_text())
    assert "e.shiftKey" in col and "const STEP" in col, "ColumnGrip's contract moved"
    grip = without_comments(GRIP.read_text())
    assert "ondblclick" in grip and "Home" in grip and "Escape" in grip, (
        "the corner and the column grip no longer share a reset"
    )


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

    body = section_fn("applyResize")
    rows = [ln for ln in body.splitlines() if "setRows(" in ln]
    assert rows, "applyResize never sets rows"
    for line in rows:
        assert "dragRows(" in line, f"applyResize sets a row count without dragRows: {line.strip()}"

    lo = int(re.search(r"MIN_ROWS = (\d+)", LAYOUT.read_text()).group(1))
    assert lo >= 1, "MIN_ROWS is itself the sentinel"


def test_the_card_follows_the_pointer_rather_than_leaping():
    """A row cap applies PER TABLE and a plot height per ROW of charts, so the
    card grows by a multiple of what was dragged -- six tables on Temperatures,
    three chart rows on System Activity. Without dividing by that multiple the
    corner runs away from the pointer."""
    body = section_fn("applyResize")
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


def test_reset_puts_the_heights_and_the_caps_back():
    """The rule layout.reset() already states for hidden sections and switched
    off columns: one control has to put everything back."""
    src = without_comments(LAYOUT.read_text())
    body = src[src.index("  reset() {") :]
    body = body[: body.index("\n  }")]
    assert "this.plotHeights = {}" in body, "reset leaves dragged plot heights in place"
    assert "this.rows = {}" in body, "reset leaves dragged row caps in place"
    assert "PLOT_HEIGHT_KEY" in body, "reset leaves the stored heights on disk"


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


def test_the_module_is_declared_rather_than_discovered():
    """A card's height is counted in table rows, so the row has to be a unit
    and not an outcome. It was 25px by accident -- padding around a
    `line-height: normal` box, where "normal" is whatever the font's metrics
    say -- and a fallback font would have shifted every card on the page."""
    css = APP_CSS.read_text()
    for token in ("--row-line", "--row-pad", "--row-rule", "--row-unit"):
        assert token in css, f"{token} is not declared"
    assert re.search(r"--row-unit:\s*calc\(", css), "the module does not state its arithmetic"

    for name in ("ModelsTable", "ProcessTable", "NetworkTable", "NetworkPanel", "ThermalPanel"):
        src = (FRONTEND / "components" / f"{name}.svelte").read_text()
        assert "py-[5px]" not in src, f"{name} still hard-codes the row padding"
        assert "var(--row-pad)" in src, f"{name} does not take its padding from the module"
        assert "var(--row-line)" in src, f"{name} still lets its line box come from the font"


def test_the_module_is_not_read_straight_off_the_custom_property():
    """getComputedStyle().getPropertyValue('--row-unit') returns the literal
    `calc(14px + 2 * 5px + 1px)` -- custom properties are substituted, not
    computed -- so parseFloat gives NaN and any fallback beside it hides the
    fact that the token was never read at all."""
    body = section_fn("rowUnit")
    assert "getPropertyValue" not in body, (
        "rowUnit parses the custom property, which yields NaN for a calc()"
    )
    assert "var(--row-unit)" in body, "rowUnit does not resolve the declared module"


def test_the_measure_coalescer_survives_a_background_tab():
    """MEASURED BUG. requestAnimationFrame does not run in a hidden or occluded
    tab. With a `queued` flag guarding it, one notification arriving in the
    background set the flag, the frame never came, the flag never cleared, and
    every later mutation returned early for the life of the component -- the
    card's span froze at 4 while the card grew to 668px and overlapped its
    neighbour. This dashboard's job is to sit on a second monitor."""
    src = without_comments(SECTION.read_text())
    effect = src[src.index("$effect(() => {") :]
    effect = effect[: effect.index("});")]
    assert "requestAnimationFrame" not in effect, "the coalescer stalls in a background tab"
    assert "setTimeout" in effect, "nothing coalesces the measurements"


def test_the_span_cannot_feed_its_own_measurement():
    """A span that made the card taller would be read on the next pass as the
    new natural height and grow again every frame. effect_update_depth_exceeded
    is compiled out of production builds, so it throws in dev and spins
    silently for a reader."""
    block = css_block(SECTION, ".slot {")
    assert "align-self: start" in block, (
        "the card fills its span, so measuring it feeds the span that set it"
    )


def test_the_gap_is_inside_the_span():
    """A 16px row-gap between 25px tracks puts every card after the first at
    25n + 16, which is never on the grid -- and being on the grid is the point."""
    zone = css_block(APP, ".zone {")
    assert "grid-auto-rows: var(--row-unit)" in zone, "the zone is not a module grid"
    assert "row-gap: 0" in zone, "a row gap would push every card off the grid"
    assert "margin-bottom" in css_block(SECTION, ".slot {"), (
        "nothing separates the cards"
    )


def test_there_is_no_band_mode_left():
    """Aligned mode was the other half of a trade the module grid removed: it
    bought rows that line up across a band by stretching the shorter card, and
    the grid lines them up without stretching anything. A second layout regime
    that is strictly worse is a second thing to maintain and a second thing to
    explain."""
    for path in (LAYOUT, APP, SECTION, SETTINGS):
        src = without_comments(path.read_text())
        for token in ("bandMode", "BandMode", "BAND_MODE", "quantised"):
            assert token not in src, f"{path.name} still refers to {token}"
    assert "subgrid" not in without_comments(APP.read_text()), (
        "the aligned-mode subgrid rule survives"
    )


def test_a_card_can_be_held_taller_than_its_content():
    """With two independent columns, holding a card taller than it needs is the
    only way to line their BOTTOMS up. A card's span is otherwise ceil(content),
    so dragging past the last row did nothing: Models has eleven models, and
    past a cap of eleven the card stopped dead under the pointer."""
    src = without_comments(LAYOUT.read_text())
    for name in ("cardSpan(", "setCardSpan(", "clearCardSpan("):
        assert name in src, f"the store has no {name}"

    apply = section_fn("applyResize")
    assert "setCardSpan(" in apply, "the gesture never pins a height"
    assert "setRows(" in apply or "setPlotHeight(" in apply, (
        "the gesture stopped driving the card's content"
    )

    block = css_block(SECTION, ".slot {")
    assert "min-height" in block, "nothing makes the card fill the height it was held to"
    assert "--card-rows" in block, "the fill is not driven by the card's span"

    src = without_comments(SECTION.read_text())
    assert re.search(r"cardRows = \$derived\(Math\.max\(naturalRows, layout\.cardSpan\(id\)\)\)", src), (
        "the span is not max(content, held), so one of the two cannot win"
    )


def test_the_held_height_is_never_written_from_a_measurement():
    """The held value is a constant the reader supplied. A height derived from
    the measured card would be read back as the new natural height and grow
    again every frame -- and effect_update_depth_exceeded is compiled out of
    production builds, so it spins silently for a reader."""
    measure = section_fn("measure")
    assert "setCardSpan" not in measure, "measure() writes the held height, which feeds itself"
    src = without_comments(SECTION.read_text())
    effect = src[src.index("$effect(() => {") :]
    effect = effect[: effect.index("});")]
    assert "setCardSpan" not in effect, "an effect writes the held height"


def test_a_gesture_that_moved_nothing_pins_nothing():
    """MEASURED BUG: every pointermove that had not yet crossed a module
    boundary -- including the one a plain click produces -- wrote
    `startSpan + 0` and silently held the card at its current height. Found by
    discovering stored spans for four cards nobody had dragged."""
    apply = section_fn("applyResize")
    head = apply[: apply.index("rowUnit()")] if "rowUnit()" in apply else apply
    assert re.search(r"if \(modules === 0\) return;", head), (
        "a zero-module gesture still writes a held height"
    )


def test_reset_releases_the_held_height():
    """Leaving it would reset the card's CONTENT to its default while the frame
    stayed wherever it had been dragged -- a reset that visibly does not."""
    body = section_fn("onResizeReset")
    assert "clearCardSpan(" in body, "double-click leaves the card pinned"

    src = without_comments(LAYOUT.read_text())
    block = src[src.index("  reset() {") :]
    block = block[: block.index("\n  }")]
    assert "this.cardSpans = {}" in block, "reset leaves held heights in place"
    assert "CARD_SPAN_KEY" in block, "reset leaves held heights on disk"


def test_every_gap_between_cards_is_the_same():
    """REGRESSION, reported from a screenshot and then measured: 18, 21, 23, 32,
    34px between consecutive cards where there had been a uniform 16.

    A card left at its natural height inside a taller span puts the
    quantisation slack -- up to one whole module -- into the gap BELOW it, so
    no two gaps match. Filling the span moves that slack inside the card, under
    its own content where it reads as padding."""
    block = css_block(SECTION, ".slot {")
    assert "min-height" in block, "the card does not fill its span, so the slack lands in the gap"
    assert "margin-bottom: 16px" in block, "nothing declares the gap"

    # `align-self: start` stays, and is not in tension with the fill -- see
    # test_the_span_cannot_feed_its_own_measurement. Start-alignment is what
    # makes the lifted measurement return the CONTENT's height rather than the
    # grid area's; min-height is what makes the card occupy the whole span.
    # Remove either and the gaps go uneven again, for opposite reasons.
    assert "align-self: start" in block, "the lifted measurement would read the grid area"


def test_the_span_is_measured_with_the_fill_lifted():
    """The card fills its span, so reading its rendered height reads the span
    straight back. That is not a runaway -- it is a RATCHET: a card whose
    content shrank would keep the height it once needed for ever. Verified
    live by switching Network Activity from charts to a table and back:
    684 -> 609 -> 684."""
    body = section_fn("measure")
    assert "minHeight = '0px'" in body, "the natural height is measured through the fill"
    assert body.index("minHeight = '0px'") < body.index("getBoundingClientRect"), (
        "the fill is lifted after the measurement, not before"
    )
    assert "naturalRows" in body, "measure() does not record the content's own height"


def test_the_gap_between_cards_is_declared_exactly_once():
    """MEASURED REGRESSION: full-width cards sat 32px apart while cards in a
    column sat 16px apart, because the slot's own margin stacked on the gap
    `.sections` was still applying between bands. Every card carries its own
    gap now, so no container may add another."""
    sections = css_block(APP, ".sections {")
    assert re.search(r"gap:\s*0", sections), ".sections adds a second gap between bands"

    zone = css_block(APP, ".zone {")
    assert re.search(r"row-gap:\s*0", zone), ".zone adds a second gap between cards"

    slot = css_block(SECTION, ".slot {")
    assert "margin-bottom: 16px" in slot, "the card no longer carries its own gap"


def test_the_module_grid_reaches_full_width_bands_too():
    """A full-width band is a zone as well, and it renders outside `.cols`. When
    the module grid lived on `.cols.packed > .zone` it never applied there, so
    the rhythm restarted at every full-width card."""
    zone = css_block(APP, ".zone {")
    assert "grid-auto-rows: var(--row-unit)" in zone, (
        "the module grid is scoped to something narrower than every zone"
    )
