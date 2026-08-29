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
PICK = FRONTEND / "components" / "PickMenu.svelte"
COLMENU = FRONTEND / "components" / "ColumnMenu.svelte"


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

    # ROWS of tables, not the NUMBER of tables. Temperatures pairs its five
    # domains two-across on a wide card, so one more row grows the card by three
    # table-heights there and five when they are stacked. Counting tables would
    # make the corner under-move by however many share a line.
    bodies = measure[measure.index("mode = 'rows'") :]
    assert "new Set(bodies.map((b) => b.offsetTop)).size" in bodies, (
        "tables sharing a line are counted separately, so the corner under-moves"
    )
    assert "bodies.length" not in bodies, "the raw table count is still what scales the drag"


def test_a_dragged_row_count_survives_a_reload():
    """The cap used to be validated against ROW_CHOICES. The grip drags it
    continuously, so 13 rows is now askable -- and list validation would have
    discarded every dragged value on reload, silently, the card simply back at
    its default."""
    src = without_comments(LAYOUT.read_text())
    reader = src[src.index("function readRows") :][:900]
    assert "ROW_CHOICES.includes" not in reader, "readRows still rejects off-list values"
    assert "clampRows(" in reader, "readRows does not clamp what it loads"


def test_settings_no_longer_duplicates_the_resize_corner():
    """The width toggle and the rows-before-paging select answered the same two
    questions the corner now answers, and answered them worse: away from the
    card, where neither can actually be judged. They are gone, and so is
    everything that existed only to serve them -- `rowOptions`, `ROW_CHOICES`
    and `PAGED_SECTIONS`.

    Show/hide stays. It is not a size question, and there is no gesture for it:
    a hidden card has no corner to drag."""
    src = without_comments(SETTINGS.read_text())
    assert "toggleWidth" not in src, "the width toggle is still in settings"
    assert "setRows" not in src and "rowChoice" not in src, "the row cap is still in settings"
    assert "rowOptions" not in src, "the select's helper outlived the select"
    assert "toggleHidden" in src, "show/hide was removed too, and it should not be"

    layout = without_comments(LAYOUT.read_text())
    for dead in ("ROW_CHOICES", "PAGED_SECTIONS"):
        assert dead not in layout, f"{dead} has no callers left but is still exported"


def test_an_uncapped_layout_saved_earlier_still_loads():
    """`0` means uncapped and nothing can produce one any more -- it came from
    the settings list, and a drag is floored at MIN_ROWS so shrinking a card can
    never flip it to "show everything". A layout saved before that still has to
    round-trip."""
    src = without_comments(LAYOUT.read_text())
    body = src[src.index("rowsFor(id: string)") :]
    body = body[: body.index("\n  }")]
    assert "=== 0 ? Infinity" in body, "a stored 0 no longer reads as uncapped"


def test_a_stored_plot_height_is_clamped_on_the_way_in():
    """A hand-edited or stale value is the same hazard readRows guards: a
    4000px plot would push every control that could undo it off the page."""
    src = without_comments(LAYOUT.read_text())
    # "function clampPlot(" with the paren: clampPlotRows now precedes it and
    # shares the prefix, so a bare "function clampPlot" matched the wrong one.
    reader = src[src.index("function readPlotHeights") : src.index("function clampPlot(")]
    assert "clampPlot(" in reader, "readPlotHeights does not clamp what it loads"

    clamp = src[src.index("function clampPlot(") :][:300]
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
    # Bounded by the <style> block, not by the first </div>: the markup inside
    # this branch contains elements of its own, and slicing on a closing tag cut
    # the guard short the moment one was added.
    branch = src[src.index("{#if collapsed}") : src.index("<style>")]
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


def test_the_two_axes_are_locked_apart():
    """THE FAILURE THIS EXISTS FOR: height is continuous and width is one
    discrete flip, and flipping the width changes the card's content layout --
    Temperatures pairs its domains when wide -- which changes its natural
    height, which invalidates the span and the scale `onstart` captured. A
    diagonal drag doing both would corrupt the height it was computing.

    It also stops a horizontal wobble during a height drag from reflowing the
    page under the reader's hand."""
    body = without_comments(GRIP.read_text())
    down = body[body.index("function onpointerdown") : body.index("function onkeydown")]
    assert "axis" in down, "the gesture never commits to an axis"
    assert re.search(r"axis = Math\.abs\(dx\) > Math\.abs\(dy\) \? 'x' : 'y'", down), (
        "the axis is not chosen by which way the pointer actually went"
    )
    assert re.search(r"if \(axis === 'y'\) \{\s*onmove\(dy\);\s*return;", down), (
        "a height-locked gesture can still reach the width path"
    )


def test_a_width_drag_aims_before_it_commits():
    """`Section` states the rule for moving a card -- nothing rearranges while
    it is being aimed at -- and a width flip is a bigger rearrangement than a
    move. It must show what a release would do and do nothing else."""
    body = without_comments(GRIP.read_text())
    down = body[body.index("function onpointerdown") : body.index("function onkeydown")]
    move = down[down.index("const move") : down.index("const stop")]
    assert "oncommit" not in move, "the width flip applies mid-drag instead of on release"
    assert "onaim(" in move, "nothing previews what the release will do"

    assert re.search(r"const ARM = (\d+)", body), "no threshold before a width aim counts"
    arm = int(re.search(r"const ARM = (\d+)", body).group(1))
    lock = int(re.search(r"const LOCK = (\d+)", body).group(1))
    assert arm > lock * 3, (
        f"ARM {arm} is too close to LOCK {lock} -- a card would flip width on a nudge"
    )


def test_a_cancelled_width_gesture_abandons():
    """MEASURED BUG in the first cut: pointerup and pointercancel were bound to
    one handler, so an interrupted gesture -- the browser taking the pointer
    back, a touch leaving the screen -- flipped the card anyway. Height needs no
    such care because it applies as it goes; width is aimed, so it has something
    to abandon."""
    body = without_comments(GRIP.read_text())
    down = body[body.index("function onpointerdown") : body.index("function onkeydown")]
    assert "const cancel" in down, "cancel is not handled separately from release"
    cancel = down[down.index("const cancel") :]
    cancel = cancel[: cancel.index(";") + 1]
    assert "oncommit" not in cancel, "a cancelled gesture still commits the flip"

    assert "addEventListener('pointercancel', cancel)" in down, (
        "pointercancel is still wired to the release handler"
    )


def test_a_width_change_releases_the_held_height():
    """A card pinned to 45 rows at half width is absurd at full width, where its
    content reflows shorter. The height was chosen for a width that no longer
    applies."""
    body = section_fn("onWidthCommit")
    assert "clearCardSpan(" in body, "the held height survives a width change"
    assert "toggleWidth(" in body, "the width never actually changes"
    assert body.index("clearCardSpan") < body.index("toggleWidth"), (
        "the height is cleared after the reflow rather than before it"
    )


def test_width_is_inert_where_there_are_no_columns():
    """Below 1100px the zones stack and every card is full width whatever its
    placement says. A flip there would change a stored value and nothing a
    reader can see."""
    src = without_comments(SECTION.read_text())
    assert "min-width: 1100px" in src, "nothing checks that there are columns to move between"
    assert "function wideEnough" in src, "the breakpoint test has no single home"

    meaningful = section_fn("meaningful")
    assert "wideEnough()" in meaningful, "an aim can arm where width means nothing"
    assert "canWiden" in meaningful, (
        "a full-width card could arm for wider, or a half-width one for narrower"
    )


def test_the_width_cue_draws_the_TARGET_footprint():
    """Outlining the card itself says "something will change" and not "it will
    become this wide" -- and width is the entire point of the gesture. The cue
    has to be the box a release would produce."""
    src = without_comments(SECTION.read_text())
    assert "function targetBox" in src, "nothing computes the footprint a release would produce"
    assert "class=\"ghost\"" in src, "no cue is drawn"

    ghost = css_block(SECTION, ".ghost {")
    assert "position: absolute" in ghost, (
        "an in-flow cue would move the card it is measured from"
    )
    assert "pointer-events: none" in ghost, "the cue would swallow the drag it is previewing"

    box = section_fn("targetBox")
    assert "columnFor(" in box, "the cue guesses a column instead of asking where the card goes"


def test_the_cue_and_the_move_agree_on_the_column():
    """A preview that worked out the destination separately could disagree with
    the move it is previewing. One answer, two callers."""
    src = without_comments(LAYOUT.read_text())
    assert "columnFor(id: string)" in src, "the destination has no single home"

    toggle = src[src.index("toggleWidth(id: string)") :]
    toggle = toggle[: toggle.index("\n  }")]
    assert "this.columnFor(id)" in toggle, "toggleWidth picks its column some other way"
    assert "#emptierColumn()" not in toggle, "toggleWidth still resolves the fallback itself"


def test_the_cue_finds_the_page_by_walking_up_from_the_card():
    """MEASURED BUG: `document.querySelector('.sections')` finds Settings' own
    three `<ol class="sections">` first -- they come earlier in the document and
    measure 0x0, so the preview rendered as a 2px sliver at the window's left
    edge. A class name is not a unique address."""
    box = section_fn("targetBox")
    assert "slotEl.closest('.sections')" in box, (
        "the page container is queried from the document rather than from the card"
    )
    assert "document.querySelector" not in box, (
        "a document-wide query can match Settings' own markup"
    )


def test_a_resized_card_makes_the_layout_non_default():
    """FOUND BY THE BUG SWEEP: isDefault checked `rows` but not `plotHeights` or
    `cardSpans`. A chart card's drag writes only those two, so its "reset
    layout" button never appeared and Settings' reset stayed disabled -- the
    card could be dragged and then only un-dragged corner by corner."""
    src = without_comments(LAYOUT.read_text())
    body = src[src.index("get isDefault()") :]
    body = body[: body.index("\n  }")]
    for store in ("rows", "plotHeights", "cardSpans"):
        assert f"Object.keys(this.{store}).length === 0" in body, (
            f"a change to {store} does not make the layout non-default, so it cannot be reset"
        )


def test_widening_lands_the_card_where_the_reader_saw_it():
    """REPORTED FROM PRODUCTION: RDMA ports at the bottom of the left column was
    dragged full width, and the two cards at the TOP of the right column jumped
    below it. A column's contents are `order` filtered by zone, so where a
    right-column card sits in `order` relative to a left-column one is
    invisible -- and a card going full width becomes a band boundary at exactly
    that invisible position.

    The rule: every card in the band whose top edge is above this one's stays
    above; the card lands right after the last of them. Verified live in three
    positions, each round-tripping."""
    src = without_comments(SECTION.read_text())
    commit = section_fn("onWidthCommit")
    assert "widen()" in commit, "widening still goes through toggleWidth, at the hidden order position"

    body = section_fn("widen")
    assert "getBoundingClientRect().top" in body, "the anchor is not chosen from what is visually above"
    assert "placeAt(id, 'full'" in body, "the card is not re-ordered as it goes full width"
    assert "layout.order.filter" in body, (
        "the cards above are not resolved through page order, so the anchor could be any of them"
    )
    # Narrowing must NOT re-order: filtering order back into the column is
    # what makes the round trip land where the card left.
    assert "toggleWidth(id)" in commit, "narrowing no longer uses toggleWidth"


def test_a_chart_card_paginates_below_the_plot_floor():
    """Network Activity in chart mode floored at 584px: eleven interface charts
    in three rows, none able to drop below the 80px plot minimum. Tables obey
    "content that does not fit paginates"; chart grids were the exception. Now
    plots shrink to the floor, then ROWS per page are cut and a pager appears."""
    src = without_comments(SECTION.read_text())
    body = section_fn("resizePlots")
    assert "MIN_PLOT_PX" in body, "the plot floor is not what switches regimes"
    assert "setPlotRows(" in body, "shrinking never cuts chart rows"
    # Shrink order: plots first, rows only past the floor.
    shrink = body[body.index("const room") :]
    assert shrink.index("setPlotHeight(") < shrink.index("setPlotRows("), (
        "rows are cut before the plots have reached the floor"
    )

    for card, name in ((TRENDS, "Trends"), (NETWORK, "NetworkTrends")):
        c = without_comments(card.read_text())
        assert "data-rows-total" in c, f"{name} does not tell Section how many rows it would draw"
        assert "<Pager" in c and "chart pages" in c, f"{name} has no pager for its charts"
        assert "new TableView" in c, f"{name} pages some other way than tables do"


def test_growing_restores_rows_before_it_grows_plots():
    """The round trip has to land where it started. Shrinking takes plots first
    then rows, so growing must give rows back first then plots -- and with the
    SAME rounding. MEASURED BUG: floor() on the way up restored nothing for a
    100px step against a 115px row, so every step went into the plots and rows
    stayed cut while plots climbed 80 -> 330."""
    body = section_fn("resizePlots")
    grow = body[body.index("if (deltaPx >= 0)") : body.index("const room")]
    assert "rowsTotal - rows0" in grow, "growing does not know how many rows are missing"
    assert "Math.round(deltaPx" in grow, "growing rounds differently from shrinking"
    assert "Math.floor(deltaPx" not in grow, "floor() cannot restore a row from one step"
    assert grow.index("setPlotRows(") < grow.index("setPlotHeight("), (
        "plots grow before the missing rows come back"
    )
    assert "resetPlotRows(" in grow, "a fully restored grid still carries a cap"


def test_the_row_budget_is_shared_across_a_card_s_divisions():
    """MEASURED: a per-grid cap did not shrink the card. Fabric has exactly two
    rows, so a cap of two cut nothing, Management's single row kept itself, and
    the card sat at three rows however far it was dragged. Divisions take rows
    from one budget, each reserving a row for every division after it so none
    loses its pager."""
    src = without_comments(NETWORK.read_text())
    effect = src[src.index("let remaining = plotRows") :]
    effect = effect[: effect.index("});")]
    assert "remaining - after" in effect, "divisions do not reserve rows for those after them"
    assert "Math.max(1," in effect, "a division can be left with no rows and no pager"
    assert "remaining -= rows" in effect, "the budget is not consumed as divisions take from it"

    section = section_fn("measure")
    assert ".reduce(" in section and "rowsTotal" in section, (
        "Section does not sum the grids' rows, so it cannot tell when all are back"
    )


def test_chart_rows_are_counted_from_the_viewport():
    """Each plot sits inside its chart's own position: relative wrapper, so
    offsetTop is measured from that wrapper and every plot reports the same
    number -- three rows counted as one, which both mis-scaled the drag and
    made the row budget arithmetic start from the wrong place."""
    body = section_fn("measure")
    plot = body[body.index("mode = 'plot'") : body.index("mode = 'rows'")]
    assert "getBoundingClientRect().top" in plot, "chart rows are counted with offsetTop"
    assert "offsetTop" not in plot, "offsetTop survives in the chart-row count"


def test_plot_rows_reset_and_count_as_non_default():
    src = without_comments(LAYOUT.read_text())
    body = src[src.index("get isDefault()") :]
    body = body[: body.index("\n  }")]
    assert "Object.keys(this.plotRows).length === 0" in body, "a row cap does not make the layout resettable"
    reset = src[src.index("  reset() {") :]
    reset = reset[: reset.index("\n  }")]
    assert "this.plotRows = {}" in reset and "PLOT_ROWS_KEY" in reset, "reset leaves the row cap behind"
    corner = section_fn("onResizeReset")
    assert "resetPlotRows(" in corner, "double-click leaves the chart rows cut"


def test_the_metric_chips_became_a_menu():
    """Twenty toggle chips took three rows of System Activity -- about 130px,
    measured -- which at the default plot height is a whole row of charts. The
    picker is a fly-out of checkboxes now, sharing PickMenu with the column
    picker so the two menus on the page cannot drift apart."""
    trends = without_comments(TRENDS.read_text())
    assert 'class="picker"' not in trends, "the chip strip is still rendered"
    assert "<PickMenu" in trends, "System Activity has no metric menu"
    assert "text=" in trends[trends.index("<PickMenu") :][:400], (
        "the metric trigger has no text -- a primary control that only appears on hover"
    )
    col = without_comments(COLMENU.read_text())
    assert "<PickMenu" in col, "ColumnMenu does not share the menu it was extracted into"
    assert "addEventListener('pointerdown'" not in col, (
        "ColumnMenu still carries its own close-on-outside logic alongside PickMenu's"
    )


def test_the_last_metric_cannot_be_switched_off():
    """Trends.toggle refuses to empty the selection -- an empty chart area reads
    as broken rather than as a choice. The menu has to SAY so rather than let a
    click silently do nothing."""
    trends = without_comments(TRENDS.read_text())
    items = trends[trends.index("const metricItems") :][:500]
    assert "selected.length === 1" in items, "the last metric is not recognised as last"
    assert "disabled: last" in items, "the last metric can still be unchecked"
    assert "'last one'" in items, "the lock is silent"


def test_the_menu_closes_from_outside_and_from_escape():
    """Not modal and no backdrop: glanced at and dismissed. The failure mode of
    getting this wrong is a menu that will not close."""
    pick = without_comments(PICK.read_text())
    assert "addEventListener('pointerdown'" in pick, "no outside-click close"
    assert "e.target instanceof Node && host.contains(e.target)" in pick, (
        "the inside/outside test casts instead of checking, and contains() on a non-Node is not reliably falsy"
    )
    assert "'Escape'" in pick and ".trigger')?.focus()" in pick, "Escape does not return focus to the trigger"


def test_a_labelled_menu_sizes_to_its_content():
    """MEASURED: fr tracks inside an absolutely positioned, shrink-to-fit box
    resolved to nothing -- the metric menu was 90px wide with every label
    clipped. max-content tracks, and a width to match."""
    block = css_block(PICK, ".host.labelled .menu {")
    assert "max-content" in block, "the labelled menu's columns can collapse to nothing"
    assert "minmax(0, 1fr)" not in block, "fr tracks in a shrink-to-fit box collapse"
