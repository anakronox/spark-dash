"""Type sizes come from the scale, not from a literal.

AB1's premise, and it was proven while closing it: `app.css` tokenised spacing,
corners and every colour but not type, so the scale existed in practice and
nowhere in code. Its table recorded 8, 15, 19, 20, 22 and 30px as the one-offs.

**By the time AB1 was finished a 13px had appeared** — in `ThermalPanel`, after
that table was written, and nothing caught it. That is the whole argument for
this file: a scale nobody can violate accidentally is worth more than a scale
everybody agrees with.

The one-offs were adjudicated rather than swept: 30/22/19/15 are a real display
scale and got names, the two 20px close buttons are one control-glyph role, the
13px was drift and folded to `--text-body`, and the 8px sort arrow stays a
literal because it is a glyph rather than text — recorded at its use.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "frontend" / "src"
APP_CSS = SRC / "app.css"

#: Arbitrary sizes allowed to stay, and why. An entry here is a decision; the
#: test exists so adding one is deliberate rather than accidental.
ALLOWED_ARBITRARY = {
    "components/SortButton.svelte": {
        "text-[8px]": "a sort arrow: aria-hidden, a glyph rather than text, "
        "deliberately below the interface scale",
    }
}


def sources() -> list[Path]:
    return [p for p in SRC.rglob("*") if p.suffix in (".svelte", ".css") and p.is_file()]


def test_no_raw_font_size_literals():
    """`font-size: 11px` is how the scale drifts: it looks local and correct,
    and nothing relates it to the other thirty places that meant the same
    thing."""
    offenders = []
    for p in sources():
        for m in re.finditer(r"font-size:\s*([0-9.]+(?:px|rem))", p.read_text()):
            offenders.append(f"{p.relative_to(SRC)}: font-size: {m.group(1)}")
    assert not offenders, "use a --text-* token instead:\n  " + "\n  ".join(offenders)


def test_arbitrary_tailwind_sizes_are_declared_exceptions():
    """`text-[13px]` bypasses the scale exactly like a CSS literal does."""
    for p in sources():
        rel = str(p.relative_to(SRC))
        for m in re.finditer(r"text-\[[0-9.]+(?:px|rem)\]", p.read_text()):
            assert m.group(0) in ALLOWED_ARBITRARY.get(rel, {}), (
                f"{rel} uses {m.group(0)}. Either use a --text-* token, or add it to "
                "ALLOWED_ARBITRARY with the reason it is not part of the scale."
            )


def test_every_token_used_is_defined():
    """A `var(--text-typo)` renders at the UA's 16px with no error anywhere —
    the same silent-loss shape AB2 hit five times during the migration."""
    defined = set(re.findall(r"^\s*(--text-[a-z-]+):", APP_CSS.read_text(), re.M))
    used = set()
    for p in sources():
        used |= set(re.findall(r"var\((--text-[a-z-]+)\)", p.read_text()))
    assert used <= defined, f"undefined type tokens: {sorted(used - defined)}"


def test_the_tokens_live_in_theme_so_utilities_generate():
    """They must be inside `@theme`, not merely in `:root`. Tailwind v4 emits a
    `text-*` utility for every token in the `--text-*` namespace, which is what
    lets markup and CSS name the same size — and is what the migration would
    otherwise have forced into arbitrary values."""
    css = APP_CSS.read_text()
    theme = css[css.index("@theme inline {") : css.index("\n}", css.index("@theme inline {"))]
    for token in ("--text-body", "--text-label", "--text-micro", "--text-nano"):
        assert token in theme, f"{token} is outside @theme, so no utility is generated"


@pytest.mark.parametrize(
    "token", ["--text-hero", "--text-headline", "--text-title", "--text-title-sm", "--text-glyph"]
)
def test_the_display_scale_survives(token):
    """These were AB1's "one-offs". They are deliberate — the page's hierarchy
    is built from them — so folding them into the interface scale would flatten
    it. Pinned so a later tidy does not mistake considered for stray."""
    assert token in APP_CSS.read_text(), f"{token} was removed"
