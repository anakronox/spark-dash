"""Every history chip must have a query behind it, and vice versa.

This is a bug that has already happened here: a chip shipped whose key had no
matching entry in HISTORY_QUERIES, so clicking it did nothing at all — it
neither showed nor hid a chart, because there was never a series to draw. It
was reported as "the Throughput chip does nothing".

The two lists live in different languages and different directories, which is
exactly the shape that drifts. Nothing else checks it.
"""

from __future__ import annotations

import re
from pathlib import Path

from spark_dash_backend.prometheus import HISTORY_QUERIES, NODE_FILTERABLE

HISTORY_TS = Path(__file__).resolve().parent.parent / "frontend" / "src" / "lib" / "history.ts"

#: Queries deliberately not offered as a chip. `memory_used_bytes` backs the
#: absolute figure the tooltip shows beside the percentage chart, so it is
#: fetched without ever being a chip of its own.
QUERIES_WITHOUT_CHIPS = {
    "memory_used_bytes",
    # A property of the DEPLOYMENT, not of a node: it sums components that run
    # on the monitoring host and carries no `node` label, so it cannot be a
    # per-node chart. Read once in the settings panel, where the rest of the
    # deployment's facts already live.
    "monitoring_bytes",
}


def chip_keys() -> list[str]:
    src = HISTORY_TS.read_text()
    start = src.index("export const METRICS")
    end = src.index("\n];", start)
    return re.findall(r"key:\s*'([a-z0-9_]+)'", src[start:end])


def test_every_chip_has_a_query():
    """A chip without one renders a control that cannot do anything."""
    missing = sorted(set(chip_keys()) - set(HISTORY_QUERIES))
    assert not missing, f"chips with no HISTORY_QUERIES entry: {missing}"


def test_every_query_is_reachable():
    """A query nobody can select is dead weight that still costs a fetch."""
    unreachable = sorted(set(HISTORY_QUERIES) - set(chip_keys()) - QUERIES_WITHOUT_CHIPS)
    assert not unreachable, f"queries with no chip: {unreachable}"


def test_chip_keys_are_unique():
    keys = chip_keys()
    assert len(keys) == len(set(keys)), "duplicate chip keys would render two identical controls"


def test_node_filterable_entries_are_bare_selectors():
    """NODE_FILTERABLE means "a {node=...} matcher can be appended".

    Appending one to an aggregation is not valid PromQL — `(sum by (node)
    (x)){node="y"}` does not parse — and would surface as a 503 from Prometheus
    rather than an honest error here. So membership is checked against the
    shape of the expression, not trusted.
    """
    for key in NODE_FILTERABLE:
        expr = HISTORY_QUERIES[key]
        assert re.fullmatch(r"[a-z_:]+[a-z0-9_:]*", expr), (
            f"{key} is in NODE_FILTERABLE but is not a bare selector: {expr!r}"
        )


def test_aggregations_are_not_marked_filterable():
    for key, expr in HISTORY_QUERIES.items():
        if any(tok in expr for tok in ("sum by", "max by", "avg by", "rate(", "100 *")):
            assert key not in NODE_FILTERABLE, (
                f"{key} is an aggregation and cannot take an appended node matcher"
            )
