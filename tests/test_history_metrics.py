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

LIB = Path(__file__).resolve().parent.parent / "frontend" / "src" / "lib"
HISTORY_TS = LIB / "history.ts"
NETWORK_TS = LIB / "network-history.ts"

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


def network_keys() -> list[str]:
    """Query keys the Network history card fetches.

    A SECOND SOURCE OF REACHABILITY, added because the check below is the one
    that noticed. The network queries are not chips — that card has no metric
    picker, it draws every interface it is given — so they read as dead weight
    to a test that only knows about chips.

    Parsed from the constants rather than from NETWORK_METRICS' contents,
    because the array holds references and a regex over it would find the
    identifier and not the string it stands for.
    """
    src = NETWORK_TS.read_text()
    declared = dict(re.findall(r"export const ([A-Z_]+) = '([a-z0-9_]+)';", src))
    listed = re.findall(r"export const NETWORK_METRICS = \[([^\]]*)\]", src)
    assert listed, "NETWORK_METRICS is not where this test expects it"
    used = re.findall(r"[A-Z_]+", listed[0])
    return [declared[name] for name in used if name in declared]


def test_every_chip_has_a_query():
    """A chip without one renders a control that cannot do anything."""
    missing = sorted(set(chip_keys()) - set(HISTORY_QUERIES))
    assert not missing, f"chips with no HISTORY_QUERIES entry: {missing}"


def test_every_query_is_reachable():
    """A query nobody can select is dead weight that still costs a fetch.

    TWO CARDS REACH INTO HISTORY_QUERIES NOW. History offers a chip per metric;
    Network history has no picker and fetches a fixed four, one per direction
    and one per fault kind. A query is reachable if either card can ask for it.
    """
    unreachable = sorted(
        set(HISTORY_QUERIES)
        - set(chip_keys())
        - set(network_keys())
        - QUERIES_WITHOUT_CHIPS
    )
    assert not unreachable, f"queries nothing can request: {unreachable}"


def test_every_network_key_has_a_query():
    """The same drift the chip check exists for, on the card that has no chips.

    Worse there, in fact: a chip with no query renders a control that does
    nothing, which someone notices and reports. A bad key here fails the fetch
    and the card simply draws fewer charts — an interface quietly missing from a
    grid of fourteen is not something anyone spots.
    """
    keys = network_keys()
    assert keys, "no network query keys found — the parser has drifted"
    missing = sorted(set(keys) - set(HISTORY_QUERIES))
    assert not missing, f"network keys with no HISTORY_QUERIES entry: {missing}"


def test_network_queries_are_rated_and_in_bits():
    """AC2, pinned. These are counters exported through a GAUGE family, so
    nothing in Prometheus or in a linter will suggest `rate()` — and a chart of
    the raw counter is a monotonic ramp that says nothing about throughput.

    Bits, because the Network table above these charts converts to bits and a
    chart in bytes would disagree with it by a factor of eight with nothing on
    either saying which was which. Only the throughput pair: a fault count is
    not a bit rate.
    """
    for key in ("network_rx_bits", "network_tx_bits"):
        expr = HISTORY_QUERIES[key]
        assert "rate(" in expr, f"{key} plots a counter without rate(): {expr!r}"
        assert expr.startswith("8 * "), f"{key} is not converted to bits: {expr!r}"
    for key in ("network_errors", "network_drops"):
        expr = HISTORY_QUERIES[key]
        # `increase()`, not `rate()`. Nobody asks how many errors per second.
        # Measured: the rate form put a real burst at 0.0002/s, and the axis
        # then printed "0.0/s" at both ends of its own scale.
        assert "increase(" in expr, f"{key} should count, not rate: {expr!r}"
        assert "rate(" not in expr.replace("increase(", ""), expr
        assert "8 *" not in expr, f"{key} is a count, not a bit rate: {expr!r}"
    # The port state is a GAUGE in the ordinary sense — 1 while the port is up —
    # so a rate over it would be meaningless rather than merely unconventional.
    for key in ("rdma_port_state", "network_link_up"):
        expr = HISTORY_QUERIES[key]
        assert "rate(" not in expr, f"{key} is a state, not a counter: {expr!r}"
        assert "8 *" not in expr


def test_link_up_takes_the_minimum_over_the_bucket():
    """A flap inside one step must survive the downsample.

    A range query reports whatever the last scrape in the bucket said, so at a
    900s step a link that dropped for two minutes and came back reads as having
    been up the whole time — and the row that exists to say "this went down"
    says nothing. `min_over_time` is what makes the window's worst moment the
    one that gets reported.
    """
    expr = HISTORY_QUERIES["network_link_up"]
    assert "min_over_time(" in expr, expr
    assert "max_over_time(" not in expr, expr
    assert expr.startswith("min by"), expr


def test_network_queries_keep_their_second_dimension():
    """The whole point of the card: never summed to the node.

    A 200Gb RoCE link and a 10Gb management port added together is a number
    describing nothing, and it is the easy mistake to make here because every
    OTHER query in this file aggregates to exactly one series per node.

    Which dimension has to survive differs by what the series is about, so the
    check names it rather than looking for one string. The interface queries key
    on the netdev; the port-state query keys on the RDMA device, because several
    ports can hang off one cable and collapsing them to the interface would draw
    two ports' history as one line.
    """
    required = {
        "network_rx_bits": "by (node, interface)",
        "network_tx_bits": "by (node, interface)",
        "network_errors": "by (node, interface)",
        "network_drops": "by (node, interface)",
        "rdma_port_state": "by (node, device, port, interface)",
        "network_link_up": "by (node, interface)",
    }
    assert set(required) == set(network_keys()), (
        "a network query was added or removed without saying what its key is"
    )
    for key, grouping in required.items():
        expr = HISTORY_QUERIES[key]
        assert grouping in expr, f"{key} does not group by {grouping}: {expr!r}"


def test_port_state_joins_through_the_interface_label():
    """AC5 depends on AC1c, and this is where that dependency lives.

    Without `group_left(interface)` the state series names a device and nothing
    can say which wire that is, so the chart could only sit in a section of its
    own — away from the traffic it explains, which was the whole reason to build
    it.

    `group_left` and not a plain `on`: the info series carries labels the state
    series does not, and a one-to-one match would refuse them.
    """
    expr = HISTORY_QUERIES["rdma_port_state"]
    assert "group_left(interface)" in expr, expr
    assert "sparkdash_rdma_port_info" in expr, expr
    # `max`, not `sum`: two overlapping label variants would sum to 2, which on
    # a two-state axis is off the top of the chart.
    assert expr.startswith("max by"), expr


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
