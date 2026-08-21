"""Every table column is defined, rendered and sortable — all three or none.

The rows in these tables are data-driven: a `ColumnDef` array decides the
header and the cell order, and a `{#snippet}` renders each cell by key. That
was M4's design, chosen because hand-written `<td>`s in fixed order shift every
value into the wrong column the moment the two lists disagree — which looks
like corrupted data rather than a broken UI.

There is a quieter failure in the same family, and it shipped twice. T1 added
the `size` cell renderer, its sort value, its tooltip helper and the CSS, and
never added the `ColumnDef`. T2 did the same for `load`, including a fetch to
the load-times endpoint on every poll. Both rendered NOTHING for two days: the
array drives the row, so a key missing from it is a column that never reaches
the DOM. Nothing looks wrong — there is simply no column, and no error.

So all three lists have to agree:

  ColumnDef   what the header renders and what the column menu offers
  snippet     what the cell renders
  TableView   what the header's sort button sorts on

A key in one and not the others is always a bug, in every direction:
  - renderer without a def   -> invisible column, work that ships as nothing
  - def without a renderer   -> blank cells under a real header
  - def without a sort key   -> a sort button that does nothing when clicked
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

COMPONENTS = Path(__file__).resolve().parent.parent / "frontend" / "src" / "components"

#: Tables whose rows are ColumnDef-driven. Network holds two, so its keys are
#: checked as one pooled set -- they share a snippet namespace by convention,
#: and a key used by both tables is legitimate.
TABLES = ("ModelsTable", "ProcessTable", "NetworkPanel")


def lists(name: str) -> tuple[set[str], set[str], set[str]]:
    src = (COMPONENTS / f"{name}.svelte").read_text()
    rendered = set(re.findall(r"c\.key === '([a-z]+)'", src))
    defined = set(re.findall(r"\{\s*key: '([a-z]+)',\s*label:", src))
    sortable = set(re.findall(r"\{\s*key: '([a-z]+)',\s*value:", src))
    return defined, rendered, sortable


@pytest.mark.parametrize("table", TABLES)
def test_every_rendered_column_is_declared(table):
    """The T1/T2 bug. A cell snippet with no ColumnDef is a column that was
    built, reviewed, documented and never displayed."""
    defined, rendered, _ = lists(table)
    orphans = sorted(rendered - defined)
    assert not orphans, (
        f"{table} renders cells for {orphans} but declares no column — "
        "they will never appear"
    )


@pytest.mark.parametrize("table", TABLES)
def test_every_declared_column_renders_something(table):
    """The inverse: a header with no cell snippet leaves a blank column, which
    reads as missing data rather than a missing branch."""
    defined, rendered, _ = lists(table)
    blanks = sorted(defined - rendered)
    assert not blanks, f"{table} declares {blanks} with no cell renderer"


@pytest.mark.parametrize("table", TABLES)
def test_every_declared_column_is_sortable(table):
    """Every header carries a SortButton, so a column with no sort value is a
    control that does nothing when clicked — indistinguishable from data that
    happens to already be in order."""
    defined, _, sortable = lists(table)
    unsortable = sorted(defined - sortable)
    assert not unsortable, f"{table} declares {unsortable} with no sort value"


# --- the slack column -------------------------------------------------------
#
# `app.css` sets `table { width: 100% }`, so an auto-layout table always has
# surplus to give away, and it gives it to whichever columns can grow — in
# proportion to content, which hands most of it to the column with the longest
# strings. That is what opened a large gap beside `model` (AA).
#
# A trailing empty column with no width constraint absorbs it instead. The
# hazard is the same one M4 designed the ColumnDef loops to prevent: a header
# and a body that disagree about how many cells a row has. Here it would be one
# extra `<th>` with no matching `<td>`, which shifts nothing visibly at first
# and misaligns the last column's borders.

SLACK_TABLES = {
    "ModelsTable": 1,
    "ProcessTable": 1,
    # Two tables in one component: RDMA ports and interfaces.
    "NetworkPanel": 2,
}


#: The Tailwind migration (roadmap AB) expresses these as utilities rather than
#: CSS, and the migration is deliberately phased — so during it these guards
#: must accept BOTH forms. That is a real cost of the migration worth recording:
#: every guard written against CSS text has to learn a second spelling, and a
#: guard that only knew the old one would go quietly green on a converted
#: component while the behaviour was gone.
def slack_cells(src: str) -> tuple[int, int]:
    """(headers, cells) marking the trailing slack column, either spelling."""
    # `SLACK_TH` / `SLACK_TD` rather than one `SLACK`: the slack cell needs the
    # same base as every other cell and differs only in width, which one shared
    # constant could not express -- see the slack-cell guard below.
    ths = src.count('<th class="slack">') + src.count("<th class={SLACK_TH}>")
    tds = src.count('<td class="slack">') + src.count("<td class={SLACK_TD}>")
    return ths, tds


@pytest.mark.parametrize("table,count", sorted(SLACK_TABLES.items()))
def test_header_and_body_agree_on_the_slack_column(table, count):
    """One `<th class="slack">` per table, and exactly one matching `<td>`.

    Unbalanced is the failure that matters: an extra header cell with no body
    cell misaligns every border on the last column, and an extra body cell
    silently widens rows past their header."""
    src = (COMPONENTS / f"{table}.svelte").read_text()
    ths, tds = slack_cells(src)
    assert ths == tds == count, (
        f"{table}: {ths} slack headers, {tds} slack cells, expected {count} of each"
    )


@pytest.mark.parametrize("table", sorted(SLACK_TABLES))
def test_the_slack_column_is_excluded_from_the_sizing_rule(table):
    """Real columns size to their content; the slack column must not, or there
    is nothing left to absorb the surplus and the gap comes straight back."""
    src = (COMPONENTS / f"{table}.svelte").read_text()
    css_form = "th:not(.slack)" in src and "td:not(.slack)" in src
    # Converted form: clipping lives on the cell constants and the slack column
    # is the only one left unsized.
    util_form = "const SLACK" in src and "w-auto" in src
    assert css_form or util_form, (
        f"{table} sizes columns without leaving the slack column to absorb the surplus"
    )
    assert "width: auto" in src or "w-auto" in src, (
        f"{table}'s slack column does not absorb anything"
    )


@pytest.mark.parametrize("table", sorted(SLACK_TABLES))
def test_slack_is_not_a_declared_column(table):
    """It carries no data, so it must not appear in ColumnDef — a reader could
    otherwise hide it from the column menu and get the gap back with no way to
    understand why."""
    defined, rendered, sortable = lists(table)
    assert "slack" not in defined | rendered | sortable


# --- AA2/AA3: fixed layout, declared widths, and the grip --------------------
#
# `table-layout: fixed` is what makes a declared width actually apply — in auto
# layout it is only a suggestion and content can override it, so a dragged
# column would sometimes spring back and read as the drag not working. Under
# fixed layout a column with NO width takes whatever is left over, which is the
# very failure AA1 fixed, so every column must declare one.

LIB = Path(__file__).resolve().parent.parent / "frontend" / "src" / "lib"
GRIP = COMPONENTS / "ColumnGrip.svelte"


def without_comments(src: str) -> str:
    """Strip CSS and HTML comments.

    Both of the checks below first passed against a file where the DECLARATION
    had been deleted and only the comment explaining it remained — the string
    was still present, so the assertion held while the behaviour was gone.
    Testing the tests is what caught that."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"<!--.*?-->", "", src, flags=re.S)


@pytest.mark.parametrize("table", TABLES)
def test_layout_is_fixed_so_declared_widths_apply(table):
    src = without_comments((COMPONENTS / f"{table}.svelte").read_text())
    # `table-fixed` is the utility spelling. Comments are stripped first for
    # the same reason as before: the explanation mentions the property, and
    # this exact check once passed against a file where only the comment
    # survived. It caught a real regression during the AB migration, where the
    # `table` rule was dropped wholesale and the widths silently stopped
    # applying.
    assert "table-layout: fixed;" in src or "table-fixed" in src, (
        f"{table} declares column widths that auto layout is free to ignore"
    )
    assert "<colgroup>" in src, f"{table} has no colgroup to carry the widths"


@pytest.mark.parametrize("table", TABLES)
def test_every_column_declares_a_width(table):
    """Under fixed layout an undeclared column takes the leftover space, which
    is exactly how one column swallowed the table before AA1."""
    src = (COMPONENTS / f"{table}.svelte").read_text()
    missing = [
        m.group(1)
        for m in re.finditer(r"\{ key: '([a-z]+)', label:[^}]*\}", src)
        if "width:" not in m.group(0)
    ]
    assert not missing, f"{table} columns with no default width: {missing}"


@pytest.mark.parametrize("table", TABLES)
def test_a_dragged_width_wins_over_the_default(table):
    """The stored pixel width has to take precedence, or dragging appears to do
    nothing after a re-render."""
    src = (COMPONENTS / f"{table}.svelte").read_text()
    assert ".width(c.key) !== null" in src, (
        f"{table} does not prefer a stored width over the ColumnDef default"
    )
    assert "c.width}ch" in src, f"{table} does not fall back to the ch default"


def test_the_grip_cannot_trigger_a_sort():
    """THE hazard: the header is already a button. SortButton fills the <th>,
    so a handle that let its events through would re-sort the table on every
    resize — and a 3px mis-aim would reorder the data being measured."""
    src = without_comments(GRIP.read_text())
    # Specifically in the POINTERDOWN path. Checking the file as a whole passed
    # against a version where pointerdown let events through and only the
    # keyboard handler still stopped them — which is the exact bug: the mouse
    # is what aims at a 8px target next to a button.
    down = src[src.index("function onpointerdown"):]
    down = down[: down.index("\n  }")]
    assert "e.stopPropagation();" in down, "the grip lets a drag reach the sort button"
    assert "e.preventDefault();" in down


def test_the_grip_is_reachable_without_a_mouse():
    """This page has been careful about that elsewhere. A resize only a mouse
    could reach would be the one exception."""
    src = GRIP.read_text()
    assert 'role="separator"' in src and 'tabindex="0"' in src
    assert "ArrowLeft" in src and "ArrowRight" in src
    assert "aria-valuenow" in src, "a separator with no value announces nothing"


def test_a_column_dragged_to_nothing_can_be_recovered():
    """The one unrecoverable state: dragged so narrow its own handle cannot be
    grabbed again. Both escapes must exist."""
    src = GRIP.read_text()
    assert "ondblclick" in src, "no double-click reset"
    assert "onreset" in src
    store = (LIB / "columns.svelte.ts").read_text()
    assert "MIN_COLUMN_PX" in store, "no floor on how narrow a column can go"


def test_reset_restores_widths_as_well_as_visibility():
    """A column dragged to its minimum is hidden in every sense that matters,
    so a reset that restored one but not the other leaves the reader stuck with
    the half they could not see."""
    store = (LIB / "columns.svelte.ts").read_text()
    reset = store[store.index("  reset() {"):]
    reset = reset[: reset.index("\n  }")]
    assert "this.hidden = {}" in reset and "this.widths = {}" in reset


def test_stored_widths_are_clamped_where_they_are_read():
    """A width dragged on a 2560px monitor is nonsense on a 1280px laptop and
    the same browser opens both, so the stored value cannot be trusted at the
    point of use."""
    store = (LIB / "columns.svelte.ts").read_text()
    read_fn = store[store.index("function readWidths"):]
    read_fn = read_fn[: read_fn.index("\nfunction ")]
    assert "Math.min" in read_fn and "Math.max" in read_fn, (
        "readWidths does not clamp what a previous browser wrote"
    )


# --- global helper classes the Tailwind migration can silently drop ----------
#
# `app.css` defines `.num { font-variant-numeric: tabular-nums }` and
# `.dim { color: var(--ink-muted) }` as global helpers. Converting a component
# rewrites whole `class` attributes, which takes those away without any error:
# the page renders, nothing fails, and a table of readings quietly gets
# proportional digits so every column shifts as values change width.
#
# That happened for real to ModelsTable during roadmap AB phase 3 and was
# caught by eye, not by a test. This is the test.

APP_CSS = Path(__file__).resolve().parent.parent / "frontend" / "src" / "app.css"


def test_the_global_helpers_still_exist():
    """If these are ever removed from app.css the check below is meaningless,
    so it fails loudly rather than passing vacuously."""
    css = APP_CSS.read_text()
    assert ".num {" in css and "tabular-nums" in css
    assert ".dim {" in css


@pytest.mark.parametrize("table", TABLES)
def test_numeric_cells_keep_tabular_figures(table):
    """Either spelling: the global `.num` class, or `tabular-nums` on the
    converted constant. A numeric table with neither has proportional digits
    and reflows on every update."""
    # Comments stripped FIRST. Without that this passed against a file with
    # `tabular-nums` deleted, because the comment explaining why it matters
    # still said the word — the third time in this suite that a guard matched
    # its own documentation instead of the code.
    src = without_comments((COMPONENTS / f"{table}.svelte").read_text())
    has_num_class = "num" in " ".join(re.findall(r'class="([^"]*)"', src))
    has_utility = "tabular-nums" in src
    assert has_num_class or has_utility, (
        f"{table} has numeric columns with neither `.num` nor `tabular-nums` — "
        "digits will be proportional and columns will shift as values change"
    )


def header_declarations(table: str) -> str:
    """Everything that styles this table's HEADER cells, in whichever spelling
    the component currently uses -- the `const TH` utility string once it has
    been converted, or the `th` rules in its `<style>` block while it has not.

    Header and body cells were one rule in the original CSS
    (`th:not(.slack), td:not(.slack)`), and converting per-cell-type is exactly
    how half of it went missing. This reads the header half on its own so a
    guard cannot be satisfied by the body half still being right."""
    src = without_comments((COMPONENTS / f"{table}.svelte").read_text())

    # TH is now built from TH_BASE, so read both -- a guard that only saw the
    # derived constant would find no padding in it and fail for the wrong
    # reason, or worse, find nothing to check and pass.
    consts = re.findall(r"const TH(?:_BASE)? =(.*?);", src, re.S)
    style = re.search(r"<style>(.*?)</style>", src, re.S)
    # BOTH TH_BASE and TH: the padding lives on the base, the truncation on the
    # derived constant, and a guard that read only one of them would check half
    # the styling and report on all of it.
    parts = list(consts)
    if style:
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", style.group(1)):
            # A bare `th`, optionally narrowed by :not() -- not `th.slack`,
            # which is the column deliberately exempted from both rules below.
            if any(
                re.fullmatch(r"th(:not\([^)]*\))?", part.strip())
                for part in selectors.split(",")
            ):
                parts.append(body)
    return "\n".join(parts)


@pytest.mark.parametrize("table", TABLES)
def test_header_cells_truncate_like_body_cells(table):
    """Under `table-layout: fixed` a header that cannot truncate overflows its
    column instead of shortening, so a long label runs into its neighbour at
    exactly the widths the reader dragged it to."""
    declarations = header_declarations(table)
    utilities = "overflow-hidden" in declarations and "text-ellipsis" in declarations
    css = "overflow: hidden" in declarations and "text-overflow: ellipsis" in declarations
    assert utilities or css, (
        f"{table} header cells have no truncation — long labels will overflow "
        "their column rather than ellipsing"
    )


@pytest.mark.parametrize("table", TABLES)
def test_header_padding_is_explicit_on_every_side(table):
    """`padding: 0 12px 6px` says "no padding above" out loud. Utilities do not
    inherit that zero from anywhere, and with no preflight the UA's own
    `th { padding: 1px }` fills the silence — one pixel, enough to drop this
    table's header row below every other table's on the same screen."""
    declarations = header_declarations(table)
    if re.search(r"padding:\s*[^;]+;", declarations):
        return  # the shorthand sets all four sides by definition
    for axis, pattern in (("top", r"\bpt-"), ("bottom", r"\bpb-"), ("sides", r"\bpx-")):
        assert re.search(pattern, declarations), (
            f"{table} header cells set no {axis} padding — the browser default "
            "applies instead of the value this table was designed with"
        )


FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
DIST = FRONTEND / "dist" / "assets"

#: Classes already inert on `main`, before the Tailwind migration touched
#: anything -- mostly column-width classes left behind when AA2 moved widths to
#: the `<colgroup>`. Verified inert against the running page: none of them
#: resolve to a rule, and none carried anything but width. Listed rather than
#: deleted so this guard can fail on NEW orphans today; each comes off the list
#: as its component is converted.
KNOWN_INERT = {
    "ModelsTable": {"lbl"},
    # Cleared by the phase-3 conversion, as ProcessTable's were.
    "NetworkPanel": set(),
    # Cleared by the phase-3 conversion, which is when these were always
    # meant to go -- the classes went with the markup they were attached to.
    "ProcessTable": set(),
    "Settings": {"mono"},
}


def unscoped_in(css: str, token: str) -> bool:
    """Does the built CSS define this class OUTSIDE any component's scope?

    Scope-awareness is the whole point. `.count` existed in the built CSS the
    entire time it was broken -- as ProcessTable's `.count.svelte-19agkpx`,
    which says nothing about whether ModelsTable's `.count` resolves. A sweep
    that ignored the hash reported the page clean while it was visibly wrong.
    """
    pattern = re.compile(r"\." + re.escape(token) + r"(?![\w-])")
    for selectors in re.findall(r"([^{}]+)\{", css):
        for part in selectors.split(","):
            if pattern.search(part) and "svelte-" not in part:
                return True
    return False


@pytest.mark.parametrize("component", sorted(KNOWN_INERT))
def test_no_static_class_is_dead(component):
    """A class in the markup with no rule behind it renders at the BROWSER
    DEFAULT, not at anything anyone chose.

    This is the failure mode of converting a component: the `<style>` block
    goes, the `class="..."` attribute stays, and the class silently stops
    meaning anything. It cost `.count` its `font-size: 11px` -- so the Models
    summary line rendered at the UA's 16px, half again larger than the same
    line in every other panel -- and `.scroll` its `overflow-x: auto`, which
    is invisible on a wide monitor and means no horizontal scrolling at all on
    a narrow one. Nothing errored, and nothing looked broken from the source.
    """
    built = sorted(DIST.glob("*.css"))
    if not built:
        pytest.skip("no built CSS to check against — run `npm run build` in frontend/")
    # Backslashes stripped: Tailwind escapes `:` and `[` in selectors, so
    # `hover:text-ink` is written `.hover\:text-ink` and a naive match finds
    # none of the variant utilities. Getting this wrong makes the check pass
    # everything rather than fail loudly.
    css = built[-1].read_text().replace("\\", "")

    src = (COMPONENTS / f"{component}.svelte").read_text()
    style = re.search(r"<style>(.*?)</style>", src, re.S)
    style_block = style.group(1) if style else ""
    markup = src.split("</script>")[-1]

    dead = set()
    for attr in re.findall(r'class="([^"{}]*)"', markup):
        for token in attr.split():
            defined = re.compile(r"\." + re.escape(token) + r"(?![\w-])")
            if defined.search(style_block) or defined.search(APP_CSS.read_text()):
                continue
            if unscoped_in(css, token):
                continue
            dead.add(token)

    assert not (dead - KNOWN_INERT[component]), (
        f"{component} carries class(es) {sorted(dead - KNOWN_INERT[component])} "
        "that no rule defines — those elements render at browser defaults. "
        "(If this fired right after an edit, rebuild the frontend first.)"
    )


#: Tables converted to utilities. The CSS-spelled ones get the same guarantee
#: from their `th {...}` / `td {...}` element selectors, which cannot miss a
#: cell; these have to state it.
CONVERTED = ("ModelsTable", "ProcessTable", "NetworkPanel")


@pytest.mark.parametrize("table", CONVERTED)
def test_the_slack_cell_is_an_ordinary_cell_apart_from_its_width(table):
    """The slack column is UNSIZED, not unstyled.

    The original CSS said this in three rules -- `th {...}` for every header
    cell, `th:not(.slack)` for the truncation, `th.slack` for the width -- and
    collapsing them into one constant plus a bare `w-auto` for slack dropped
    the base off the one cell that never gets looked at. The visible result is
    the header underline and every row's rule stopping short of the table's
    right edge, which reads as a rendering artefact rather than a bug.
    """
    src = without_comments((COMPONENTS / f"{table}.svelte").read_text())
    for name in ("SLACK_TH", "SLACK_TD"):
        match = re.search(rf"const {name} = `([^`]*)`", src)
        assert match, f"{table} defines no {name}"
        value = match.group(1)
        base = "TH_BASE" if name.endswith("TH") else "TD_BASE"
        assert base in value, (
            f"{table}'s {name} is not built on {base} — the slack cell will "
            "render without the padding and rule every other cell has"
        )
        assert "w-auto" in value, f"{table}'s {name} lost its width"

    # And the base itself has to carry the rule, or deriving from it proves
    # nothing. This is the assertion the first version of this guard lacked.
    for name in ("TH_BASE", "TD_BASE"):
        match = re.search(rf"const {name} =\s*\n?\s*(.+?);", src, re.S)
        assert match and "border-b" in match.group(1), (
            f"{table}'s {name} sets no bottom rule — every cell derived from "
            "it, slack included, loses the line between rows"
        )
