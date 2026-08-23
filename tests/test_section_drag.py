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
    app = without_comments(APP.read_text())
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


def test_the_cluster_frame_still_spans_the_row_in_compact_mode():
    """A frame means "these nodes pool memory", so one covering part of a row
    would say something untrue about which nodes are grouped.

    The drag handle made the WRAPPER the grid item. Left on `.cluster`, the
    span would apply to something that is no longer a grid item and silently do
    nothing — the frame would shrink to one cell and the claim would be wrong.
    """
    app = without_comments(APP.read_text())
    assert re.search(r"\.node-grid\.compact > :global\(\[data-group-framed\]\)", app), (
        "the compact row-span is not on the group wrapper"
    )
    assert not re.search(r"\.node-grid\.compact \.cluster \{", app), (
        "the span is still on .cluster, which is no longer the grid item"
    )
    group = without_comments(NODE_GROUP.read_text())
    assert "data-group-framed" in group, "the wrapper never marks a framed cluster"


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
