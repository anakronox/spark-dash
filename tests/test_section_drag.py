"""The drag gestures that arrange the page's sections.

Svelte 5 runes need a compiler to execute, and this repo has no JS test runner,
so these are source-level guards on the invariants whose failure is SILENT.
The behaviour itself is verified by performing a real drag in a browser against
a running build — see the pairing item in the roadmap. A guard here that could
be satisfied by a comment is worse than no guard, so comments are stripped
before every check.
"""

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "src"
SECTION = FRONTEND / "components" / "Section.svelte"
LAYOUT = FRONTEND / "lib" / "layout.svelte.ts"
APP = FRONTEND / "App.svelte"


def without_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def test_a_card_cannot_pair_with_itself():
    """Without the guard, aiming at your own card's edge would half-width the
    card in your hand and pair it with itself — the drag would appear to
    'work' and the page would be left with one card in two places."""
    src = without_comments(SECTION.read_text())
    body = src[src.index("function pairAt") : src.index("function aim")]
    assert re.search(r"targetId\s*===\s*id", body), (
        "pairAt does not exclude the dragged card from its own targets"
    )
    store = without_comments(LAYOUT.read_text())
    pair = store[store.index("pairWith(") :][:400]
    assert re.search(r"id\s*===\s*targetId", pair), "pairWith accepts a self-pair"


def test_pairing_only_applies_to_two_full_width_cards():
    """The gesture is defined on full-width cards. Aimed at a card already in a
    column it must fall through to ordinary line aiming, or dragging within a
    column would start re-columning cards that are already columned."""
    src = without_comments(SECTION.read_text())
    body = src[src.index("function pairAt") : src.index("function aim")]
    assert "dataset.zone !== 'full'" in body, "pairAt does not require the full-width band"
    assert re.search(r"zoneOf\(id\)\s*!==\s*'full'", body), (
        "pairAt does not require the DRAGGED card to be full width"
    )


def test_the_middle_of_a_card_still_means_reorder():
    """The outer edges pair; the middle must keep insert-above/insert-below, or
    reordering full-width cards is only reachable through the 16px gaps."""
    src = without_comments(SECTION.read_text())
    body = src[src.index("function pairAt") : src.index("function aim")]
    assert "PAIR_EDGE" in body, "no edge threshold — the whole card would pair"
    edge = re.search(r"const PAIR_EDGE = ([\d.]+)", src)
    assert edge, "PAIR_EDGE is not a named constant"
    assert 0 < float(edge.group(1)) < 0.5, (
        f"PAIR_EDGE is {edge.group(1)} — at 0.5 or above the two edge bands meet "
        "and there is no middle left to aim at"
    )
    assert re.search(r"if\s*\(!side\)\s*return null", body), (
        "the middle of a card does not fall through to line aiming"
    )


def test_pairing_puts_the_two_cards_in_OPPOSITE_columns():
    """The gesture's whole point: both cards become halves. Sending only the
    dragged card to a column would leave a half-width card beside a full-width
    one with nothing across from it."""
    store = without_comments(LAYOUT.read_text())
    pair = store[store.index("pairWith(") :][:900]
    assert re.search(r"side === 'left' \? 'right' : 'left'", pair), (
        "pairWith does not give the target the opposite column"
    )
    assert re.search(r"\[id\]:\s*side", pair) and re.search(r"\[targetId\]:\s*other", pair), (
        "pairWith does not place both cards"
    )


@pytest.mark.parametrize("kind", ["line", "pair"])
def test_both_drop_shapes_are_rendered(kind):
    """The drop target is a discriminated union, and a missing branch shows as
    no affordance at all — the gesture still works, silently, with nothing on
    screen saying where the card will land."""
    src = without_comments(APP.read_text())
    assert f"kind === '{kind}'" in src, f"App.svelte renders no affordance for a '{kind}' drop"


@pytest.mark.parametrize("kind,method", [("pair", "pairWith"), ("line", "placeAt")])
def test_release_dispatches_on_the_drop_shape(kind, method):
    """A pair drop falling through to placeAt() would move the dragged card and
    silently leave the target full width.

    `placeAt`, not `place`: a drag anchors on the card it aimed at. `place`
    still serves the keyboard, where "up one within my column" really is an
    index — but an index within a zone no longer says where in the page-wide
    order a card goes, now that several bands each have a left and a right."""
    src = without_comments(SECTION.read_text())
    up = src[src.index("function onPointerUp") :][:700]
    assert f"layout.{method}(" in up, f"release never calls {method} for a '{kind}' drop"


def test_a_band_is_a_run_of_the_existing_order():
    """Bands are DERIVED, never stored. A layout saved before bands existed has
    to open correctly, which it only does if `order` and `placement` remain the
    single source of truth."""
    src = without_comments(LAYOUT.read_text())
    assert "get bands()" in src, "no band derivation"
    body = src[src.index("get bands()") :][:1200]
    assert "this.visible" in body, "bands are not derived from the visible order"
    assert "BAND_KEY" not in src and "bands = $state" not in src, (
        "bands are being stored — a saved layout from before this change would "
        "then open without them"
    )


def test_an_empty_column_still_anchors_on_its_band():
    """The one case with no visible card to aim at. Without a band anchor the
    drop falls to the end of the page-wide order — the exact bug bands fix,
    resurfacing where nothing is drawn to aim at."""
    src = without_comments(SECTION.read_text())
    body = src[src.index("function aim") :][:1400]
    assert "bandLast" in body, "an empty column does not anchor on its band"
    app = without_comments(APP.read_text())
    assert "data-band-last" in app, "the band anchor is never rendered for aim() to read"


def test_the_keyboard_shift_keeps_its_place():
    """Left/right arrow changes a card's column. It used to append to the end
    of the target zone, which sent the card to the bottom of the page — the
    keyboard's copy of the bug bands fix. Changing zone without touching order
    is what keeps it where it is."""
    src = without_comments(LAYOUT.read_text())
    body = src[src.index("shiftZone(") :][:600]
    assert "this.setZone(" in body, "shiftZone still reorders instead of re-zoning"
    assert "this.place(" not in body, "shiftZone still appends to the target zone"


def test_drop_indicators_are_matched_to_their_band():
    """Zone names repeat once there are several bands, so an indicator keyed on
    the zone alone would draw in every band at once."""
    full = without_comments(APP.read_text())
    # The column zones' markup. The node grid is a zone too (AQ), but there is
    # only one of it, so its drop cell needs no band.
    app = full[full.index("{#snippet zone(") :]
    for kind in ("line", "pair"):
        block = app[app.index(f"kind === '{kind}'") - 200 : app.index(f"kind === '{kind}'") + 40]
        assert "drop?.band === band" in block, (
            f"the '{kind}' affordance is not scoped to its band"
        )


LAYOUT_SRC = LAYOUT.read_text()
NODE_GROUP = FRONTEND / "components" / "NodeGroup.svelte"


def test_a_saved_node_order_tolerates_hardware_changing():
    """Node keys are LIVE DATA, unlike the five section ids: a key is a cluster
    name or a standalone node's id, so adding, removing or reclustering
    hardware changes the set under a saved order.

    Both directions matter and they fail differently. An unknown key kept would
    render a card for hardware that is gone; a new key dropped would HIDE a
    node that exists, because a months-old ordering never mentioned it. The
    second is the dangerous one on a monitoring dashboard."""
    src = without_comments(LAYOUT_SRC)
    body = src[src.index("orderGroups(") :][:900]
    assert "known.has(k)" in body, "unknown keys are not filtered out"
    assert re.search(r"for \(const k of keys\) if \(!seen\.has\(k\)\) out\.push\(k\)", body), (
        "keys the saved order has never seen are not appended — a node added to "
        "cluster.yml would be hidden by an old ordering"
    )


def test_reset_layout_clears_the_node_order_too():
    """The unrecoverability rule this codebase applies everywhere: anything that
    rearranges the page must be undone by the one control that puts it back."""
    src = without_comments(LAYOUT_SRC)
    body = src[src.index("  reset() {") :][:900]
    assert "this.nodeOrder = []" in body, "reset leaves the node card order in place"
    assert "NODE_ORDER_KEY" in body, "reset does not clear the stored node order"
    default = src[src.index("get isDefault()") :][:600]
    assert "this.nodeOrder.length === 0" in default, (
        "isDefault ignores the node order, so 'reset layout' would stay hidden "
        "after the cards had been rearranged"
    )


def test_a_group_spans_one_column_per_member_in_compact_mode():
    """A cluster's frame spans its members' columns, not the row, so a
    standalone node sits beside it in a frame of its own (Brian, 2026-10-12):
    the full-row frame left three of four columns empty on two rows.

    The drag handle made the WRAPPER the grid item. Left on `.cluster`, the
    span would apply to something that is no longer a grid item and silently do
    nothing. Capped at the column count, or a five-node cluster in four columns
    would add an implicit fifth.
    """
    app = without_comments(APP.read_text())
    assert re.search(
        r"\.node-grid\.compact > :global\(\[data-group\]\) \{\s*grid-column: span min\(var\(--span, 1\), var\(--cols\)\)",
        app,
    ), "the compact span is not on the group wrapper, capped at the column count"
    cluster = app[app.index(".node-grid.compact .cluster {") :]
    cluster = cluster[: cluster.index("}")]
    assert "grid-column" not in cluster, "the span is on .cluster, which is not the grid item"
    group = without_comments(NODE_GROUP.read_text())
    assert "style:--span={span}" in group, "the wrapper never says how many columns it takes"


def test_every_compact_card_is_the_same_width():
    """Measured before: 397px for a cluster member against 420px standalone,
    because members were gridded inside the frame's 13px a side. A standalone
    node is framed the same way, and members are spaced by the outer gap plus
    both frames, which puts every card on the outer column grid."""
    app = without_comments(APP.read_text())
    assert "column-gap: calc(12px + 26px)" in app, "members are not spaced to the outer columns"
    # The same element and class as a cluster's frame, so the padding, border
    # and member columns cannot drift apart.
    assert '<section class="cluster solo">' in app, "a standalone frame is not built like a cluster's"


def test_frames_on_a_row_share_their_rows():
    """Brian, 2026-10-12: the standalone frame ended short of the cluster's
    and its card sat higher than the members. Every group takes three shared
    rows, header, band and cards, through a subgrid, so frames on one row end
    level and every card starts on the third."""
    app = without_comments(APP.read_text())
    wrapper = app[app.index(".node-grid.compact > :global([data-group]) {") :]
    wrapper = wrapper[: wrapper.index("}")]
    assert "grid-row: span 3" in wrapper and "grid-template-rows: subgrid" in wrapper
    assert re.search(r"\.node-grid\.compact \.cluster \{\s*grid-row: 1 / -1;\s*grid-template-rows: subgrid", app)
    assert re.search(r"\.node-grid\.compact \.cluster > \.nodes \{\s*grid-row: 3", app), (
        "cards are not pinned to the third row, so a frame with no band lifts them"
    )


def test_node_dragging_aims_on_the_axis_the_layout_actually_uses():
    """Full width is one column; compact grids the cards 1/2/4/8 across four
    breakpoints. Aiming by y alone would be wrong at three of them, and reading
    the track count is what keeps it right without hardcoding a breakpoint."""
    group = without_comments(NODE_GROUP.read_text())
    assert "gridTemplateColumns" in group, "the aim never reads the grid's track count"
    assert re.search(r"tracks > 1", group), "the aim does not switch axis when gridded"


@pytest.mark.parametrize(
    "path,selector",
    [
        (APP, ".sections"),
        (APP, ".zone"),
        (APP, ".cols"),
        (SECTION, ".slot"),
    ],
    ids=["sections", "zone", "cols", "slot"],
)
def test_every_layout_grid_declares_a_zero_minimum_track(path, selector):
    """`minmax(0, 1fr)`, never a bare `1fr` and never an implicit track.

    App.svelte already carries a long note calling this "the whole of the
    dashboard's layout shift": an `auto`-minimum track refuses to be narrower
    than its content, so a column holding a wide table stops tracking the
    window and pushes the page sideways.

    `.slot` is here because it was added as a grid WITHOUT a track and did
    exactly that — its implicit `auto` column measured 970px inside an 860px
    half, so the Models card would not shrink with the window. Only Models
    showed it, because its declared column widths sum highest; every panel had
    the bug.
    """
    src = without_comments(path.read_text())
    block = re.search(rf"{re.escape(selector)} \{{(.*?)\}}", src, re.S)
    assert block, f"{path.name} has no `{selector}` rule"
    body = block.group(1)
    assert "display: grid" in body, f"{selector} is no longer a grid — is this guard still right?"
    track = re.search(r"grid-template-columns:\s*([^;]+);", body)
    assert track, (
        f"{selector} is a grid with no grid-template-columns — the implicit track "
        "is `auto`, whose minimum is min-content, so it will not shrink"
    )
    assert "minmax(0" in track.group(1), (
        f"{selector} uses `{track.group(1).strip()}` — a track without a 0 minimum "
        "refuses to be narrower than its content"
    )


def test_the_dashed_swatch_is_not_killed_by_the_inline_colour():
    """A `background` shorthand resets `background-image`, and an inline style
    beats a class rule — so a swatch given its colour inline and its dash in CSS
    renders solid, silently. The tooltip then keys two lines with two identical
    marks.

    Source-level because the failure is a computed style: it looks correct in
    the markup, in the stylesheet, and in a screenshot at 8px. It was caught by
    zooming into a deployed page.
    """
    src = (FRONTEND / "components" / "MetricChart.svelte").read_text()
    swatch = src[src.index('class="swatch"') : src.index("</span>", src.index('class="swatch"'))]
    assert "style:background-color=" in swatch, (
        "the swatch sets the `background` shorthand, which wipes its dash gradient"
    )
    assert "style:background=" not in swatch


def test_a_card_can_sit_beside_the_nodes():
    """AQ (Brian, 2026-10-12): when the compact node frames fit in half the
    grid, as on a wide monitor at eight columns, the right half is a page
    column, "beside the nodes", that takes half-width cards. The bands leave
    those cards out, or they would render twice."""
    layout = without_comments(LAYOUT_SRC)
    assert "'nodes'" in layout[layout.index("export type Zone") :][:80], "no nodes zone"
    bands = layout[layout.index("get bands()") :]
    bands = bands[: bands.index("flush();\n    return out;")]
    assert "=== 'nodes') continue" in bands, "a card beside the nodes also renders in a band"
    app = without_comments(APP.read_text())
    assert "{@render zone('nodes', -1, layout.inZone('nodes'), null)}" in app, (
        "the right half is not a zone like the page's columns"
    )


def test_the_top_splits_only_when_the_nodes_fit_in_half():
    """Splitting with the frames wider than half would squeeze them; the count
    comes from the same breakpoints as the grid's CSS."""
    app = without_comments(APP.read_text())
    assert "nodeSpan <= nodeCols / 2" in app
    for bp in ("2320", "1160", "600"):
        assert f"viewportWidth >= {bp}" in app and f"min-width: {bp}px" in app, (
            f"the split and the grid disagree about the {bp}px breakpoint"
        )
    assert "const availCols = $derived(splitTop ? nodeCols / 2 : nodeCols);" in app, (
        "split nodes keep the full grid's column count and squeeze"
    )


def test_one_row_of_frames_fills_the_width():
    """Brian, 2026-10-14: three nodes in a four-column grid left a quarter of
    the node area empty. A single row uses exactly the columns its frames
    need; frames that wrap keep the power-of-two count."""
    app = without_comments(APP.read_text())
    assert "nodeSpan > 0 && nodeSpan <= availCols ? nodeSpan : availCols" in app
    assert "style:--cols={layout.compactCards ? gridCols : undefined}" in app


def test_a_standalone_frame_carries_a_header_and_its_own_band():
    """So it does not sit as an empty frame beside a cluster's header and
    pooled band. Its band is the node's own memory."""
    app = without_comments(APP.read_text())
    solo = app[app.index('<section class="cluster solo">') :]
    solo = solo[: solo.index("</section>")]
    assert "memory not pooled" in solo and "<MemoryBand" in solo
    top = app[app.index("  .top.split {") :]
    assert "repeat(2, minmax(0, 1fr))" in top[: top.index("}")]


def test_frames_end_level_with_the_card_beside_them():
    """In the split top, a taller card on the right stretches the node frames
    (the stretch goes to the cards row), and the node grid carries the same
    16px bottom margin as a card slot, so both visible edges meet."""
    app = without_comments(APP.read_text())
    top = app[app.index("  .top.split {") :]
    assert "align-items: stretch" in top[: top.index("}")]
    compact = app[app.index("  .node-grid.compact {") :]
    assert "grid-auto-rows: auto auto 1fr" in compact[: compact.index("}")]
    grid = app[app.index("  .top.split > .node-grid {") :]
    assert "margin-bottom: 16px" in grid[: grid.index("}")]


def test_gauge_panels_size_and_page_like_charts():
    """System Activity's gauges (roadmap BA) are plots to the card's corner.

    Section finds a chart card's plots by selector. Without the gauge panel
    in it, a gauges copy fell back to table mode: the corner stepped by a
    table row and never paged, and nothing errored.
    """
    src = without_comments(SECTION.read_text())
    assert "[data-plot]" in src, "Section does not count gauge panels as plots"
    panel = without_comments((FRONTEND / "components" / "GaugePanel.svelte").read_text())
    assert re.search(r"data-plot\b", panel), "GaugePanel's box is not marked data-plot"
    assert "{height}px" in panel, "the gauge box does not take the card's plot height"
