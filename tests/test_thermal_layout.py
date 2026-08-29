"""The Temperatures card's use of its own width.

Same standing as test_section_drag and test_card_resize: Svelte 5 runes need a
compiler to execute and this repo has no JS runner, so these are source-level
guards on the invariants whose failure is SILENT. The layout itself is verified
by measuring a running build in a browser. Comments are stripped before every
check, because a guard a comment can satisfy is worse than no guard -- which has
already happened once in this suite.
"""

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "src"
PANEL = FRONTEND / "components" / "ThermalPanel.svelte"
THERMAL = FRONTEND / "lib" / "thermal.ts"


def without_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def css_block(path: Path, selector: str) -> str:
    src = without_comments(path.read_text())
    start = src.index(selector)
    return src[start : src.index("}", start)]


def test_something_always_absorbs_the_leftover_width():
    """Every column declares a width in `ch`, so on a card wider than their sum
    -- 87ch, about 629px -- something must take the difference or the declared
    widths stretch. That used to be a seventh column containing nothing: 154px
    of an 817px card, and more on a full-width one, empty by design.

    The bar takes it now. But the spacer was load-bearing in two cases: `bar`
    can be switched off from the ColumnMenu, and it can be given a pixel width
    by its ColumnGrip. In either case it cannot flex and the empty column has to
    come back, or the stretching returns."""
    src = without_comments(PANEL.read_text())
    assert "barFlexes" in src, "no rule decides which column absorbs the leftover width"

    rule = src[src.index("const barFlexes") :]
    rule = rule[: rule.index(");") + 2]
    assert "'bar'" in rule, "the flexible column is not the bar"
    assert "visible()" in rule, "a hidden bar would still be treated as flexible"
    assert re.search(r"width\('bar'\)\s*===\s*null", rule), (
        "a bar pinned to a pixel width would still be treated as flexible"
    )


def test_the_spacer_returns_in_every_part_of_the_table():
    """A `<col>` without its `<th>` and `<td>` is a malformed table, not a
    narrower one. All three are conditional on the same test or the columns
    stop lining up with their headers."""
    src = without_comments(PANEL.read_text())
    guards = src.count("{#if !barFlexes}")
    assert guards == 3, (
        f"the spacer is conditional in {guards} places, not 3 (colgroup, thead, tbody)"
    )
    assert "SLACK_TH" in src and "SLACK_TD" in src, "the spacer cells were deleted outright"


def test_the_domains_pair_up_on_the_CARD_s_width_not_the_viewport():
    """The same window shows this card at 817px in a column and 1700px full
    width, so the viewport stopped being a proxy for how much room it has. Two
    sensor blocks need about 1272px; only the card can answer."""
    panel = css_block(PANEL, "section.panel {")
    assert "container-type: inline-size" in panel, "the card is not a container"

    src = without_comments(PANEL.read_text())
    assert "@container" in src, "the two-column switch is not a container query"

    m = re.search(r"@container \(min-width: (\d+)px\)", src)
    assert m, "the container query has no width threshold"
    threshold = int(m.group(1))
    assert threshold >= 1272, (
        f"{threshold}px is below two 629px blocks plus the gap -- a column would "
        "push its table into the horizontal scroll box"
    )

    grid = css_block(PANEL, ".domains {")
    assert "minmax(0, 1fr)" in grid, (
        "a bare 1fr takes its minimum from the content, and these tables are "
        "wide enough to hold the column open and stop the card ever shrinking"
    )


def test_the_domain_notes_are_off_the_heading_but_not_lost():
    """They never wrapped, so they cost horizontal room in a card that had none
    and no vertical room at all. The text still explains why the GPU limit reads
    90 degrees where the package reads 104.8, so it survives as the heading's
    title rather than being deleted."""
    src = without_comments(PANEL.read_text())
    assert 'class="note dim"' not in src, "the note is still rendered as a line of text"
    assert re.search(r'<h3 class="head-row" title=\{g\.note\}>', src), (
        "the note is not carried on the heading, so the explanation is lost"
    )

    data = THERMAL.read_text()
    assert "note:" in data, "DOMAINS no longer carries the notes at all"
    assert "shutdown threshold" in data, "the GPU's limit is no longer explained anywhere"
