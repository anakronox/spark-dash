"""Every theme's chart palette must survive its own surface.

`theme.svelte.ts` records two candidate themes cut for failing these checks.
That practice was documented but not repeatable — the tool lived on one machine
and never in this repo. These tests make it enforced, so a theme that fails
cannot land and the rejections stop being folklore.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from palette_check import (
    CONTRAST_ALLOWANCES,
    ThemeBlock,
    check,
    delta_e,
    parse_themes,
)

REPO = Path(__file__).resolve().parent.parent
THEME_TS = REPO / "frontend" / "src" / "lib" / "theme.svelte.ts"


def theme_names() -> list[str]:
    return [t.name for t in parse_themes()]


def test_themes_are_discovered_from_the_stylesheet():
    """Read from app.css rather than a list kept beside it, so a theme cannot be
    added to the CSS and quietly skipped by the check."""
    names = theme_names()
    assert "dark" in names, "the :root default must be validated like any other"
    assert len(names) >= 2


@pytest.mark.parametrize("theme", parse_themes(), ids=lambda t: t.name)
def test_every_theme_passes_every_check(theme: ThemeBlock):
    failures = [f"{f.check}: {f.detail}" for f in check(theme) if not f.ok]
    assert not failures, f"{theme.name} ({theme.mode}, {theme.surface}) — " + "; ".join(failures)


def test_the_check_actually_fails_a_bad_palette():
    """The test that keeps the others honest.

    This is the green-phosphor shape that was cut: green, teal and amber
    adjacent, which collapse under protanopia and deuteranopia. If this passes,
    the checks above are decoration.
    """
    phosphor = ThemeBlock(
        name="phosphor",
        mode="dark",
        surface="#0b0f0b",
        slots=[
            "#33ff33", "#22dd88", "#66cc22", "#aacc33",
            "#88ee55", "#44bb66", "#99dd44", "#55cc99",
        ],
    )
    failures = [f.check for f in check(phosphor) if not f.ok]
    assert "cvd separation" in failures
    assert "normal-vision floor" in failures


def test_a_grey_palette_trips_the_chroma_floor():
    """The other cut candidate: a muted slate that read as grey. Greys cannot be
    told apart by hue at all, which no amount of contrast fixes."""
    slate = ThemeBlock(
        name="slate",
        mode="dark",
        surface="#15171a",
        slots=["#8a9099", "#7f858e", "#949aa3", "#767c85",
               "#9aa0a9", "#71777f", "#8f959e", "#7b818a"],
    )
    assert "chroma floor" in [f.check for f in check(slate) if not f.ok]


def test_every_registered_palette_has_css_and_vice_versa():
    """Drift between the TypeScript registry and the stylesheet.

    A `PaletteId` with no CSS block renders as the `:root` defaults with no
    error; a CSS block nobody registered is unreachable. Both have precedent in
    this repo, which is why this is asserted rather than assumed.
    """
    src = THEME_TS.read_text()
    union = re.search(r"export type PaletteId =([^;]+);", src)
    assert union, "PaletteId union not found — did the file move?"
    registered = set(re.findall(r"'([a-z]+)'", union.group(1)))

    in_css = set(theme_names())
    assert registered == in_css, (
        f"registered but no CSS: {sorted(registered - in_css)}; "
        f"CSS but unregistered: {sorted(in_css - registered)}"
    )


def test_allowances_name_real_themes_and_slots():
    """An allowance for a theme or slot that no longer exists is a silent
    exemption waiting to apply to something it was never argued for."""
    names = set(theme_names())
    for theme_name, slots in CONTRAST_ALLOWANCES.items():
        assert theme_name in names, f"allowance for unknown theme {theme_name!r}"
        for slot in slots:
            assert re.fullmatch(r"--chart-[1-8]", slot), f"not a chart slot: {slot}"


def test_delta_e_is_symmetric_and_zero_for_identical_colours():
    assert delta_e("#3987e5", "#3987e5") == pytest.approx(0.0, abs=1e-9)
    assert delta_e("#3987e5", "#da5c2b") == pytest.approx(
        delta_e("#da5c2b", "#3987e5"), abs=1e-9
    )
