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
    BAND_OVERRIDE,
    CONTRAST_ALLOWANCES,
    ThemeBlock,
    check,
    contrast,
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


# --- chrome, which the palette checker does not cover -----------------------
#
# `check()` validates the eight CHART slots against the surface. Nothing
# validated the tokens everything else is drawn in: body text, the muted tier,
# and the four status colours. A theme could pass every palette check and still
# render unreadable prose, which is most of the page.

APP_CSS = REPO / "frontend" / "src" / "app.css"

#: WCAG AA for normal-size text. The status colours are held to it too: they
#: are rendered as text (health reasons, the process table's runtime names),
#: not only as swatches.
TEXT_MIN = 4.5

TEXT_TOKENS = (
    "--ink",
    "--ink-2",
    "--ink-muted",
    "--good",
    "--warning",
    "--serious",
    "--critical",
)


def theme_tokens() -> dict[str, dict[str, str]]:
    """Every theme's fully-resolved token set, base values included.

    Read from the stylesheet for the same reason `parse_themes` is: a theme
    cannot be added to the CSS and quietly skipped by the check.
    """
    text = APP_CSS.read_text()
    blocks = re.findall(r":root(?:\[data-theme='([a-z]+)'\])?\s*\{(.*?)\n\}", text, re.S)
    base: dict[str, str] = {}
    out: dict[str, dict[str, str]] = {}
    for name, body in blocks:
        tok = {k: v.strip() for k, v in re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", body)}
        if not name:
            base = tok
            continue
        out[name] = {**base, **tok}
    return out


@pytest.mark.parametrize("name", sorted(theme_tokens()))
def test_text_and_status_clear_aa_on_every_theme(name):
    """Body text, the muted tier and the status ramp, against the panel they
    are actually drawn on. The status colours count as text here because they
    ARE text on this page — a health reason and a runtime name are printed in
    them, not just dotted."""
    tokens = theme_tokens()[name]
    panel = tokens["--panel"]
    failures = [
        (t, tokens[t], round(contrast(tokens[t], panel), 2))
        for t in TEXT_TOKENS
        if contrast(tokens[t], panel) < TEXT_MIN
    ]
    assert not failures, f"{name}: below {TEXT_MIN}:1 on {panel} -> {failures}"


def test_high_contrast_actually_is_the_highest_contrast():
    """The theme's whole justification. If it does not beat the others at
    separation it is just another dark theme with a misleading name, and a
    reader who chose it for a reason has been sold nothing.

    Its rules are checked too: everywhere else a border is a hint and
    separation comes from surface lift, which is exactly what disappears at low
    vision — so here structure has to be drawn."""
    tokens = theme_tokens()

    worst = {}
    for theme in parse_themes():
        pairs = zip(theme.slots, theme.slots[1:], strict=False)
        worst[theme.name] = min(
            min(delta_e(a, b, "protan"), delta_e(a, b, "deutan")) for a, b in pairs
        )
    others = {k: v for k, v in worst.items() if k != "contrast"}
    assert worst["contrast"] > max(others.values()) * 1.5, (
        f"high contrast separates at dE {worst['contrast']:.1f}; "
        f"the rest reach {max(others.values()):.1f} — it is not earning its name"
    )

    hc_rule = contrast(tokens["contrast"]["--rule"], tokens["contrast"]["--panel"])
    rest = [
        contrast(t["--rule"], t["--panel"])
        for n, t in tokens.items()
        if n != "contrast"
    ]
    assert hc_rule > max(rest) * 1.5, (
        f"rules at {hc_rule:.2f}:1 are not visibly stronger than the rest "
        f"(best {max(rest):.2f}:1) — structure is still only implied"
    )


def test_band_overrides_name_real_themes():
    """A typo here silently disables the lightness band for a theme that does
    not exist while leaving the real one unchecked — the failure mode is a
    check that passes because it never ran."""
    names = {t.name for t in parse_themes()}
    unknown = sorted(set(BAND_OVERRIDE) - names)
    assert not unknown, f"BAND_OVERRIDE names themes that do not exist: {unknown}"
