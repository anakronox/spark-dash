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
NETHIST = FRONTEND / "lib" / "network-history.ts"
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
    # Show/hide moved too, once the page itself could do it: the gutter's close
    # hides an original, and the add button shows it again in place.
    assert "toggleHidden" not in src and "Reset sections" not in src, "Settings still lists sections"

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


def test_the_gutter_closes_a_card_instead_of_folding_it():
    """MEASURED: collapse folded an unheld card to 59px but a HELD card stayed
    at its held height -- 659px both ways -- because the held span won over
    the stub. With held heights in use, every collapse did nothing. The gutter
    control closes now: the original hides (Settings and the add button bring
    it back in place), a copy is removed."""
    src = without_comments(SECTION.read_text())
    assert "collapsed" not in src and ".stub" not in src, "the fold-to-stub path survives"
    assert 'class="close"' in src, "no close control"
    assert "copy ? layout.remove(id) : layout.toggleHidden(id)" in src, (
        "closing does not distinguish an original (hide) from a copy (remove)"
    )
    assert "`${copy ? 'Remove' : 'Hide'} ${label}`" in src, "the label does not say which it will do"
    layout = without_comments(LAYOUT.read_text())
    assert "toggleCollapsed" not in layout and "COLLAPSE_KEY" not in layout, "collapse state survives in the store"


def test_the_module_is_declared_rather_than_discovered():
    """A card's height is counted in table rows, so the row has to be a unit
    and not an outcome. It was 25px by accident -- padding around a
    `line-height: normal` box, where "normal" is whatever the font's metrics
    say -- and a fallback font would have shifted every card on the page."""
    css = APP_CSS.read_text()
    for token in ("--row-line", "--row-pad", "--row-rule", "--row-unit"):
        assert token in css, f"{token} is not declared"
    assert re.search(r"--row-unit:\s*calc\(", css), "the module does not state its arithmetic"

    for name in ("ModelsTable", "ProcessTable", "NetworkTable", "RdmaTable", "ThermalPanel"):
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
    # The effect that OWNS the observers, not the first effect in the file --
    # a focus-management effect now precedes it.
    effect = src[src.index("new MutationObserver") - 600 : src.index("new MutationObserver")]
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
    # In paging mode the span is max(content, held); scroll mode is the span
    # alone, which test_a_scrolling_card_is_a_fixed_box... covers.
    assert re.search(r"cardRows = \$derived\(scrolling \? layout\.cardSpan\(id\) : Math\.max\(naturalRows, layout\.cardSpan\(id\)\)\)", src), (
        "the span is not max(content, held) when paging, so one of the two cannot win"
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


def test_a_row_cap_yields_when_the_card_is_held_taller_than_its_content():
    """REPORTED: the metric chips came off System Activity and freed 130px, and
    the card sat held at its old height with one row of charts paged and an
    empty band below them, because the cap and the held height are both user
    state and nothing said "there is room now". If the card is held taller than
    its content needs and rows are paged away, enough come back to use it.

    This writes layout from a measurement, which Section otherwise avoids, so
    the guard pins the three things that keep it from looping: it only raises
    the cap, only while held exceeds natural, and never past rowsTotal."""
    body = section_fn("measure")
    plot = body[body.index("mode = 'plot'") : body.index("mode = 'rows'")]
    assert "layout.cardSpan(id)" in plot, "measure never asks what the card is held at"
    assert "held > naturalRows" in plot, "the cap is touched even when there is no room"
    assert "tops.length < rowsTotal" in plot, "the cap is touched even when every row is shown"
    assert "if (extra > 0)" in plot, "a cap can be written when nothing fits"
    assert "resetPlotRows(id)" in plot, "restoring every row still leaves a cap behind"
    # `naturalRows` and not a fresh measurement: the fill is back on by then,
    # and a fresh read would return the held height and find no room.
    after = plot[plot.index("layout.cardSpan(id)") :]
    assert "getBoundingClientRect().height + GAP_PX" not in after, (
        "the room is measured through the fill, so it is always zero"
    )


def test_network_activity_has_four_interface_groups_behind_a_menu():
    """RoCE, Management, WiFi and Other, chosen from the same PickMenu the
    metric picker uses. The rule that sorts an interface into one of them is
    exercised for real under node in test_network_history; this pins the
    wiring around it."""
    src = without_comments(NETHIST.read_text())
    assert "export type Division = 'fabric' | 'management' | 'wifi' | 'other'" in src
    assert "DEFAULT_DIVISIONS: Division[] = ['fabric', 'management']" in src, (
        "a fresh install should show RoCE and Management"
    )
    assert "label: 'RoCE'" in src, "the fabric division is not labelled RoCE"

    card = without_comments(NETWORK.read_text())
    assert "<PickMenu" in card and 'what="Interface groups"' in card, "no group menu on the card"
    assert "spark-dash.network-groups.v1" in card, "the choice is not remembered"
    assert "if (!next.length) return;" in card, "the card can be emptied of every group"
    for fact in ("wireless: i.wireless", "driver: i.driver", "bus: i.bus"):
        assert fact in card, f"the live snapshot's {fact.split(':')[0]} never reaches the rule"


def test_the_agent_reports_kind_facts_not_a_verdict():
    """The rule lives in the frontend so it can change without a node redeploy;
    the agent sends the three facts a name cannot give."""
    net = (Path(__file__).resolve().parent.parent / "agent/src/spark_dash_agent/collectors/network.py").read_text()
    for fn in ("_is_wireless", "_driver", "_bus"):
        assert f"def {fn}(" in net, f"the collector has no {fn}"
    assert "wireless=self._is_wireless(name)" in net and "driver=self._driver(name)" in net and "bus=self._bus(name)" in net
    assert "if not link.is_symlink():" in net, (
        "resolve() on an absent driver symlink returns the path itself, whose basename is 'driver'"
    )
    model = (Path(__file__).resolve().parent.parent / "common/src/spark_dash_common/models.py").read_text()
    for field in ("wireless: bool = Field(", "driver: str | None = Field(", "bus: str | None = Field("):
        assert field in model, f"NetworkInterface lacks {field.split(':')[0]}"


def test_scroll_mode_makes_the_drag_the_height_and_nothing_else():
    """Paging couples height to content through the row cap; scrolling
    decouples them -- the held span IS the height, every row renders, the
    panel scrolls. So in scroll mode the gesture must write the span and never
    a cap or a plot height."""
    body = section_fn("applyResize")
    scroll = body[body.index("layout.overflow === 'scroll'") :]
    scroll = scroll[: scroll.index("}")]
    assert "setCardSpan(" in scroll and "return;" in scroll, "scroll mode falls through to the paging gesture"
    assert "setRows(" not in scroll and "setPlotHeight(" not in scroll

    src = without_comments(LAYOUT.read_text())
    for fn in ("rowsFor(id: string)", "plotRowsFor(id: string)"):
        head = src[src.index(fn) :][:220]
        assert "this.overflow === 'scroll') return Infinity" in head, f"{fn} still caps in scroll mode"


def test_a_scrolling_card_is_a_fixed_box_with_a_sticky_header():
    """`height`, not `min-height`: the resize system measures the card with
    its fill lifted, and lifting min-height changes nothing when height is
    set, so measure() reads the span straight back -- no growth, no ratchet.
    cardRows must be the span itself, not max(natural, span): natural is the
    full content and max() would grow the card to it and never scroll."""
    src = without_comments(SECTION.read_text())
    assert "scrolling ? layout.cardSpan(id) : Math.max(naturalRows" in src, (
        "a scrolling card still takes max(natural, held) and cannot be shorter than its content"
    )
    box = css_block(SECTION, ".slot.scrolling {")
    assert "height: calc(var(--card-rows" in box and "min-height: 0" in box
    panel = css_block(SECTION, ".slot.scrolling > :global(section.panel) {")
    assert "overflow-y: auto" in panel
    header = css_block(SECTION, ".slot.scrolling > :global(section.panel > header) {")
    assert "position: sticky" in header and "background: var(--panel)" in header, (
        "the header scrolls away, or the rows show through it"
    )
    assert "overscroll-behavior" not in src, "overscroll containment would trap the page under a card"
    assert "panel.tabIndex = 0" in src, "a scroll region must be focusable to scroll without a mouse"


def test_column_headers_stick_under_the_card_header_in_scroll_mode():
    """A table scrolled to its fortieth row must still say what its columns
    are. Three things conspire against a sticky `th`, and each is a silent
    failure -- the cell simply scrolls away:

    - it sticks to its nearest SCROLLING ancestor, and every wide table sits
      in an `overflow-x: auto` box, which is a scroll container even when
      nothing overflows. That box must give up its overflow in scroll mode;
    - the offset under the card header is the header's height, which is not
      a constant (its controls wrap), so it has to be measured, not typed;
    - `border-collapse` leaves the cell's borders behind when it sticks, so
      the underline has to travel as a shadow."""
    th = css_block(SECTION, ".slot.scrolling > :global(section.panel thead th) {")
    assert "position: sticky" in th and "top: var(--sticky-top" in th, (
        "column headers scroll away, or stick at a typed offset"
    )
    assert "background: var(--panel)" in th, "the rows show through the stuck header"
    assert "box-shadow: inset 0 -1px 0 var(--rule)" in th, "the header's underline stays behind"

    wrap = css_block(SECTION, ".slot.scrolling > :global(section.panel :is(.overflow-x-auto, .scroll)) {")
    assert "overflow-x: visible" in wrap, "the wide-table box is still the scroll container the th sticks to"

    header = css_block(SECTION, ".slot.scrolling > :global(section.panel > header) {")
    assert "left: 0" in header, (
        "with the panel now scrolling sideways for a wide table, the card header would scroll off"
    )

    # A sticky box cannot leave its containing block's content box, so the
    # panel's own top padding is a strip the stuck header can never cover and
    # the rows scroll by in it, above the card title. Seen in production. The
    # padding moves onto the header, and its size is read from the panel's
    # RESTING style -- with the scroll-mode class off -- because scroll mode is
    # what zeroes it.
    panel = css_block(SECTION, ".slot.scrolling > :global(section.panel) {")
    assert "padding-top: 0" in panel, "rows scroll through the panel's top padding, above the header"
    assert "padding-top: var(--panel-pad-top" in header, "the header does not take the padding over"
    measure = section_fn("measure")
    assert "classList.remove('scrolling')" in measure and "paddingTop" in measure, (
        "the padding is read while scroll mode has already zeroed it"
    )

    measure = section_fn("measure")
    assert "setProperty('--sticky-top'" in measure and "offsetHeight" in measure, (
        "the sticky offset is not measured from the card header"
    )

    # Every horizontal scroll box in a card must be one of the two classes the
    # rule above releases, or its th sticks to nothing.
    for path in sorted((FRONTEND / "components").glob("*.svelte")):
        src = without_comments(path.read_text())
        for m in re.finditer(r"([^{}]+)\{[^{}]*overflow-x:\s*auto", src):
            selector = m.group(1).strip().split("\n")[-1].strip()
            assert selector == ".scroll", f"{path.name}: `{selector}` scrolls sideways but is not `.scroll`"


def test_overflow_is_a_setting_that_resets():
    src = without_comments(LAYOUT.read_text())
    body = src[src.index("get isDefault()") :]
    body = body[: body.index("\n  }")]
    assert "this.overflow === 'page'" in body, "scroll mode does not make the layout resettable"
    reset = src[src.index("  reset() {") :]
    reset = reset[: reset.index("\n  }")]
    assert "setOverflow('page')" in reset
    settings = without_comments(SETTINGS.read_text())
    assert "setOverflow('scroll')" in settings and "setOverflow('page')" in settings, "no control in Settings"


def test_rdma_ports_is_a_view_of_network_activity_not_a_card():
    """Same subject -- the fabric, by port instead of by interface. The ports
    table's unique facts (state, link layer, NEGOTIATED RATE, per-port errors)
    live on as Network Activity's third view; the card is retired, and a saved
    layout that still names it is dropped by the store's known-id filter."""
    layout = without_comments(LAYOUT.read_text())
    assert "{ id: 'network'," not in layout, "the RDMA Ports card is still a section"
    assert not (FRONTEND / "components" / "NetworkPanel.svelte").exists(), "NetworkPanel survives"
    assert (FRONTEND / "components" / "RdmaTable.svelte").exists()
    card = without_comments(NETWORK.read_text())
    assert "const MODES: Mode[] = ['charts', 'table', 'ports']" in card
    assert "<RdmaTable" in card and "new ColumnView('network.rdma'" in card, (
        "the ports view does not reuse the RDMA table, or lost its column choices"
    )
    # A port is RoCE by definition and a live table has no time range: the
    # groups menu and the history controls hide in that view.
    assert "disabled={mode === 'ports'}" in card, "the groups menu and history controls do not disable in the ports view"
    app = without_comments(APP.read_text())
    assert "NetworkPanel" not in app and "id === 'network'" not in app


def test_every_store_reader_keeps_an_instance_id():
    """THE FAILURE INSTANCES EXIST TO AVOID: a stored `kind#2` that a reader
    drops on load as unknown is a card that silently vanishes. Readers must
    validate the KIND of an id, never the id itself."""
    src = without_comments(LAYOUT.read_text())
    assert "export function kindOf(id: string)" in src
    assert "known.has(id)" not in src, "a reader still validates the bare id -- a copy would be dropped on load"
    assert src.count("known.has(kindOf(id))") >= 7, "not every reader validates by kind"
    # reconcile appends a KIND with no instance, not an id missing from the list
    rec = src[src.index("export function reconcile") :]
    rec = rec[: rec.index("\n}")]
    assert "fromSaved.map(kindOf)" in rec, "a copy would be treated as missing and its kind re-appended"


def test_a_copy_has_its_own_view_state():
    """Six pieces of state are the component's rather than the layout's -- a
    chosen view, the groups, metrics, events. Two copies sharing one storage
    key would fight over it: divergent live, last-writer-wins on reload."""
    src = without_comments(LAYOUT.read_text())
    assert "export function instanceKey(key: string, id: string)" in src
    for path, keys in ((TRENDS, ("trend-metrics", "trend-events")),
                       (NETWORK, ("network-mode", "network-quiet", "network-events", "network-groups"))):
        c = without_comments(path.read_text())
        for k in keys:
            assert f"instanceKey('spark-dash.{k}.v1', instance)" in c, f"{path.name}: {k} is not keyed per instance"
        assert "instance?: string" in c, f"{path.name} takes no instance"
    app = without_comments(APP.read_text())
    assert app.count("instance={id}") == 2, "App does not tell both chart cards which instance they are"
    assert "{@const kind = kindOf(id)}" in app and "{#if kind === '" in app, "App still dispatches on the raw id"


def test_copies_can_be_made_and_removed_and_reset_clears_them():
    src = without_comments(LAYOUT.read_text())
    dup = src[src.index("  duplicate(id: string)") :]
    dup = dup[: dup.index("\n  }")]
    assert "while (this.order.includes(`${kind}#${n}`)) n++" in dup, "a second copy would collide with the first"
    assert "[copy]: this.zoneOf(id)" in dup, "a copy does not land in the same column"
    rem = src[src.index("  remove(id: string)") :]
    rem = rem[: rem.index("\n  }")]
    assert "if (!isCopy(id)) return;" in rem, "the original can be removed, leaving a kind with no card"
    assert "purgeInstanceKeys(id)" in rem, "a removed copy leaves its view state behind"
    reset = src[src.index("  reset() {") :]
    reset = reset[: reset.index("\n  }")]
    assert "purgeInstanceKeys" in reset, "reset drops copies but keeps their view state"
    # The controls live on the page: + ELEMENT adds, the gutter's close removes.
    section = without_comments(SECTION.read_text())
    assert "layout.remove(id)" in section
    app = without_comments(APP.read_text())
    assert "layout.addCard(kind)" in app


def test_network_activity_reads_its_stored_view_back():
    """MEASURED BUG: MODES was declared after `chosenMode = $state(readMode())`.
    readMode runs inside that initialiser and reads MODES, which is in its
    temporal dead zone at that moment; the ReferenceError was swallowed by
    readMode's own try/catch and every card came back on its automatic view
    with its stored choice ignored. Silent until a copy made it visible."""
    src = without_comments(NETWORK.read_text())
    assert src.index("const MODES: Mode[]") < src.index("let chosenMode = $state<Mode | null>(readMode())"), (
        "MODES is declared after the initialiser that reads it -- a TDZ error the try/catch swallows"
    )


def test_the_page_has_an_add_element_button_that_lands_the_card():
    """Left of Settings, filled in the accent: the one control on the bar that
    does something rather than opens something. Picking a kind copies its last
    visible card, or shows it if every copy is hidden; then the page scrolls
    to it and lifts it -- on a long page a click with nothing visibly
    happening reads as broken. The scroll fires twice because the new slot
    spans one module until Section has measured it, and the page below it
    reflows: a single scroll at mount landed 950px short, measured."""
    app = without_comments(APP.read_text())
    assert 'text="element"' in app and 'mode="action"' in app and 'tone="accent"' in app, "no add button"
    assert "layout.countOf(s.id)" in app, "the menu does not say how many of each kind the page has"
    fn = app[app.index("async function addCard") :]
    fn = fn[: fn.index("\n  }")]
    assert fn.count("scrollIntoView") >= 1 and fn.count("goto(") >= 2, "the scroll does not retry after the grid settles"
    assert "layout.landed = id" in fn, "the new card is not lifted"

    src = without_comments(LAYOUT.read_text())
    add = src[src.index("  addCard(kind: string)") :]
    add = add[: add.index("\n  }")]
    assert "this.duplicate(visible[visible.length - 1])" in add, "a visible kind is not copied after its last instance"
    assert "this.toggleHidden(id)" in add, "a wholly hidden kind is copied instead of shown"

    pick = without_comments(PICK.read_text())
    assert "mode === 'action'" in pick and "<button" in pick, "PickMenu has no action rows"
    block = css_block(PICK, ".host.accent .trigger {")
    assert "background: var(--good)" in block, "the accent trigger is not filled"


def test_the_add_menu_hangs_from_the_right_and_uses_the_page_type():
    """REPORTED: the add menu opened rightward from the far right of the bar,
    ran past the viewport and gave the page a horizontal scrollbar; and its
    rows came up in a bigger, different typeface. The first was a labelled
    trigger defaulting to open rightward; the second was `font: inherit` on
    the action rows -- the shorthand resets the size .row had just set."""
    app = without_comments(APP.read_text())
    add = app[app.index('text="element"') :][:300]
    assert 'align="end"' in add, "the add menu opens rightward from the right edge of the page"
    pick = without_comments(PICK.read_text())
    assert "class:end={hangsEnd}" in pick and ".host.end .menu" in pick, "PickMenu cannot hang from its end"
    block = css_block(PICK, "button.row {")
    assert "font: inherit" not in block, "font: inherit resets the row's size to the bar's"
    assert "font-family: inherit" in block


def test_the_add_menu_counts_what_is_on_the_page():
    """REPORTED: hide the only Network Activity and the add menu still said
    "1" beside it -- it counted every instance in `order`, hidden included.
    A number beside a card nobody can see counts something the reader
    cannot; "0" is what invites the click that shows it."""
    src = without_comments(LAYOUT.read_text())
    body = src[src.index("  countOf(kind: string)") :]
    body = body[: body.index("\n  }")]
    assert "!this.isHidden(id)" in body, "countOf counts hidden instances"


def test_settings_calls_them_elements():
    """The page's own button says + ELEMENT, so the settings that shape them say
    the same word; a control named one thing on the page and another in
    Settings is two things to learn. The per-interface alerting note is gone
    -- it restated what the documentation already covers, once per node."""
    src = SETTINGS.read_text()
    assert '<h3 class="eyebrow dim">Elements</h3>' in src and '<h3 class="eyebrow dim">Node elements</h3>' in src
    assert '>Cards<' not in src and '>Node cards<' not in src
    assert "Unticked stops" not in src


def test_the_chart_cards_controls_are_one_segmented_primitive():
    """The view switch, the range and the toggles on both chart cards used to
    be per-component rules: each option its own outlined button, the chosen
    one differing from its neighbours only by text colour. One border around
    the set now, no borders between options, and the chosen one filled the
    way Settings' Elements / Node elements selectors are."""
    css = APP_CSS.read_text()
    seg = css_block(APP_CSS, ".segmented {")
    assert "border: 1px solid var(--rule)" in seg and "overflow: hidden" in seg
    inner = css_block(APP_CSS, ".segmented > button {")
    assert "border: 0" in inner, "options still carry their own borders"
    active = css_block(APP_CSS, ".segmented > button.active {")
    assert "background: var(--fill)" in active and "color: var(--ink)" in active, (
        "the chosen option is not filled the way Settings' choices are"
    )
    settings_active = css_block(SETTINGS, ".choice.active {")
    assert "background: var(--fill)" in settings_active, "Settings' choices changed; keep the two in step"
    for path in (TRENDS, NETWORK):
        c = path.read_text()
        assert 'class="ranges segmented"' in c, f"{path.name} range group is not the primitive"
        assert 'class="events toggle"' in c, f"{path.name} events toggle is not the primitive"
        assert ".range.active" not in c and ".events.on" not in c, f"{path.name} still styles these itself"
    assert 'class="modes segmented"' in NETWORK.read_text()


def test_the_chart_card_controls_hold_their_place_across_views():
    """REPORTED: the view switch moved between charts, table and ports because
    the idle toggle appeared only on charts and events + range vanished on
    ports, and the row is right-aligned -- a reader who learned where "table"
    was found it elsewhere after switching. Everything renders in every view
    and disables when it does not apply."""
    card = without_comments(NETWORK.read_text())
    for gone in ("{#if mode !== 'ports'}",):
        assert gone not in card, f"a control still comes and goes: {gone}"
    assert card.count("disabled={mode === 'ports'}") >= 3, "events, the range and the groups menu are not all disabled on ports"
    css = APP_CSS.read_text()
    assert "--fill: color-mix(in srgb, var(--rule) 72%, var(--ink))" in css, "the selected fill is not a step up from the rule"
    for sel in (".segmented > button.active {", ".toggle.on {"):
        assert "background: var(--fill)" in css_block(APP_CSS, sel)
    assert "background: var(--fill)" in css_block(SETTINGS, ".choice.active {"), "Settings' choices and the page's controls diverged"


def test_the_pager_sits_at_the_bottom_of_a_held_card():
    """A card held taller than its content left its prev/next floating
    mid-card with empty space beneath, so the control moved every time the
    height did. The panel is a column and its trailing pager takes the slack
    -- only a pager that is the panel's last direct child, so multi-table
    cards keep each pager under its own table."""
    panel = css_block(APP_CSS, ".slot > section.panel {")
    assert "flex-direction: column" in panel
    assert "margin-top: auto" in css_block(APP_CSS, ".slot > section.panel > nav:last-child {")
    # A flex item's minimum width is its content's: without this a table's
    # scroll box stops shrinking with the card and pushes past the page edge.
    assert "min-width: 0" in css_block(APP_CSS, ".slot > section.panel > * {")


def test_idle_lives_in_the_groups_menu_not_on_the_card():
    """Brian: no language about showing idle interfaces on the card's face.
    The "N idle" toggle beside the view switch and the heading for an
    all-idle group are gone; the groups menu carries both -- "all idle"
    beside a group that would draw nothing, and an "Idle links" toggle row."""
    card = without_comments(NETWORK.read_text())
    assert "idle</button>" not in card, "the idle chip is still on the card"
    assert "silentGroups" not in card, "the all-idle heading survives"
    assert "'all idle'" in card and "label: 'Idle links'" in card, "the groups menu does not carry idle"
    assert "key === '__idle'" in card, "the idle row does not toggle idle links"

def test_a_copy_wears_its_number():
    """Two Network Activity cards look identical, and the accessible name
    ("Network Activity 2") is not something a sighted reader sees. The copy
    carries its instance number on the card frame, always visible -- a copy
    is a copy whether or not the pointer is near it -- and the number is the
    same one the label uses, sliced from the same id."""
    src = without_comments(SECTION.read_text())
    assert "{#if copy}" in src, "nothing on the frame says which copy this is"
    block = src[src.index("{#if copy}") :]
    block = block[: block.index("{/if}")]
    assert 'class="badge"' in block and "id.slice(id.indexOf('#') + 1)" in block, (
        "the badge does not show the instance number"
    )
    css = css_block(SECTION, ".badge {")
    assert "opacity" not in css, "the badge is hover-revealed; a copy must say so at rest"
    assert "position: absolute" in css and "pointer-events: none" in css, (
        "the badge is in the flow, or intercepts the header's controls"
    )


def test_the_width_cue_speaks_in_the_theme_s_accent():
    """There is no `--accent` token; the accent is `--good`, and the resize
    corner, the `+ element` button and a just-landed card all use it. The
    ghost used `--warning` -- a status colour, the same amber on every theme,
    so on Forest, Paper and High Contrast it was a colour nothing else on the
    page spoke. A width flip is not a warning."""
    ghost = css_block(SECTION, ".ghost {")
    assert "var(--good)" in ghost and "var(--warning)" not in ghost, "the ghost is not in the theme's accent"
    grip = css_block(GRIP, ".grip.armed {")
    assert "var(--warning)" not in grip, "the armed corner still turns amber"


def test_a_fresh_system_activity_card_shows_four_metrics():
    """One GPU-utilization line stretched across a full-width card was the
    first thing a newcomer saw. Four now, one of each kind the card can
    show, every key filtered against METRICS so a rename cannot leave an
    empty chart."""
    src = without_comments(TRENDS.read_text())
    block = src[src.index("const DEFAULT_SELECTION") : src.index("];", src.index("const DEFAULT_SELECTION"))]
    keys = re.findall(r"'(\w+)'", block)
    assert len(keys) == 4, f"the default is {len(keys)} metrics, not 4"
    history = (FRONTEND / "lib" / "history.ts").read_text()
    for k in keys:
        assert f"key: '{k}'" in history, f"default metric {k} is not in METRICS"
    fn = src[src.index("function readSelection") :]
    fn = fn[: fn.index("\n  }\n")]
    assert "DEFAULT_SELECTION.filter((k) => known.has(k))" in fn, "the default is not checked against METRICS"
    assert "METRICS[0]" not in fn, "still falls back to the first metric alone"


def test_reset_layout_leaves_the_theme_alone():
    """Reset is a layout reset. A theme is a choice about the reader's eyes
    and screen, not about the arrangement of cards, and stripping it as a
    side effect of tidying the page would be a surprise every time."""
    src = without_comments(LAYOUT.read_text())
    assert "theme" not in src.lower().replace("themes", ""), "the layout store knows about the theme at all"
    reset = src[src.index("  reset() {") :]
    reset = reset[: reset.index("\n  }")]
    assert "spark-dash.theme" not in reset and "THEME" not in reset, "reset touches the theme"
