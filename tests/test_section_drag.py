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
