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


APP_SVELTE = Path(__file__).resolve().parent.parent / "frontend" / "src" / "App.svelte"


def cluster_sum_block() -> str:
    """The body of the `cluster` derivation, comments stripped.

    Comments here quote the numbers that motivate the rule (`47,672`,
    `tokens_per_sec`), so a check that did not strip them would match its own
    reasoning rather than the code — the failure mode this suite has hit four
    times."""
    src = APP_SVELTE.read_text()
    start = src.index("const cluster = $derived.by(")
    end = src.index("return { tokensPerSec", start)
    block = src[start:end]
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    return re.sub(r"//[^\n]*", "", block)


def test_the_headline_sums_decode_only():
    """Y1's whole finding: prefill and decode differ by three orders of
    magnitude, and adding them made the headline read 47,672 tok/s while the
    model generated 48. The combined `tokens_per_sec` field still exists and is
    still emitted — recorded history is written against it — so nothing stops
    someone summing it here by reaching for the obvious name."""
    block = cluster_sum_block()
    assert "generation_tokens_per_sec" in block, "the headline no longer sums decode"
    assert re.search(r"(?<!generation_)(?<!prompt_)\btokens_per_sec\b", block) is None, (
        "the cluster headline sums the COMBINED tokens_per_sec — that is the "
        "number that reads 47,672 while the model generates 48"
    )


def test_prefill_is_summed_separately_from_the_headline():
    """Reported beside the headline, never into it."""
    block = cluster_sum_block()
    assert "prompt_tokens_per_sec" in block, "prefill is not collected"
    for line in block.splitlines():
        if "prompt_tokens_per_sec" in line:
            assert "tokensPerSec" not in line, (
                f"prefill is being added to the headline: {line.strip()}"
            )


def test_prefill_reads_as_a_state_not_a_bare_rate():
    """Non-zero 1% of the time and five to six digits when it fires. A bare
    number here reads '0' nearly always and then dwarfs the decode figure
    beside it, which is Y1's bug in a smaller font."""
    src = APP_SVELTE.read_text()
    marker = "<dt class={DT}>prefill</dt>"
    assert marker in src, "no prefill entry in the summary row"
    facts = src[src.index(marker) :][:600]
    assert "idle" in facts, "prefill shows no resting state"
    assert "compact(" in facts, "prefill renders a full-precision rate"
