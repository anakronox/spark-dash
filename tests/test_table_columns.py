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
