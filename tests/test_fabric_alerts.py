"""Every fabric alert honours the interface exclusions, and says why it can.

The rules and the flag they join against live in different languages and
different directories — `alerts.yml` here, the agent's exporter there — which
is the shape that drifts. The drift is silent in both directions:

  - a rule that forgets the join goes on paging about a port someone
    deliberately unplugged, which is the failure this whole area was added for
  - a join written with a bare `unless` instead of `on (...)` subtracts
    nothing, because the agent's series carry `job` and `instance` labels as
    well, and full-label matching would compare all of them
  - a join written as `and <flag> == 1` matches nothing at all while the flag
    is missing — which it is on every node until the new agent is rolled out —
    so link alerting would go silent instead of merely un-filtered. Measured
    against live Prometheus 2026-08-21: 0 series against 4

Both were live on 2026-08-21: four `NetworkLinkDown` and four `RdmaPortDown`
firing across sparketa and sparkjr for two pulled cables per node.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ALERTS_YML = Path(__file__).resolve().parent.parent / "central" / "config" / "alerts.yml"

#: alert name -> (the flag it must join against, the labels it must join on).
#: RDMA joins on device+port rather than on interface: the flag is DERIVED by
#: the agent from the netdev the RoCE device is paired with, so the rule never
#: has to know that pairing.
GUARDED = {
    "NetworkLinkDown": ("sparkdash_network_monitored", ("node", "interface")),
    "NetworkErrorsRising": ("sparkdash_network_monitored", ("node", "interface")),
    "RdmaPortDown": ("sparkdash_rdma_port_monitored", ("node", "device", "port")),
    "RdmaErrorsRising": ("sparkdash_rdma_port_monitored", ("node", "device", "port")),
}


def fabric_rules() -> dict[str, str]:
    groups = yaml.safe_load(ALERTS_YML.read_text())["groups"]
    return {
        rule["alert"]: rule["expr"]
        for group in groups
        for rule in group.get("rules", [])
        if "alert" in rule
    }


def test_every_fabric_alert_is_gated_on_the_monitored_flag():
    rules = fabric_rules()
    for name, (flag, _) in GUARDED.items():
        assert name in rules, f"{name} has gone missing from alerts.yml"
        assert flag in rules[name], f"{name} does not honour {flag}"


def test_exclusion_is_subtraction_so_a_missing_flag_fails_noisy():
    """`unless <flag> == 0`, never `and <flag> == 1`.

    The flag exists only once every node runs an agent that exports it. `and
    == 1` matches nothing while it is absent, so a stale node — or a rules
    reload landing before a rollout finishes — would lose link alerting
    entirely. Subtraction removes only what is explicitly marked excluded, so
    an absent flag leaves the previous behaviour untouched."""
    rules = fabric_rules()
    for name, (flag, _) in GUARDED.items():
        assert "unless on" in rules[name], f"{name} does not subtract {flag}"
        assert f"{flag} == 0" in rules[name], f"{name} must exclude on == 0"
        assert "and on (" not in rules[name], (
            f"{name} joins with `and`, which fails silent when {flag} is absent"
        )


def test_the_join_names_its_labels():
    """`unless on (...)`, never a bare `unless`. Full-label matching against a
    different metric family subtracts nothing, so every exclusion would be
    quietly ignored."""
    rules = fabric_rules()
    for name, (flag, labels) in GUARDED.items():
        match = re.search(r"unless on \(([^)]*)\)\s*" + re.escape(flag), rules[name])
        assert match, f"{name} subtracts {flag} without `on (...)`"
        assert [x.strip() for x in match.group(1).split(",")] == list(labels)


def test_link_down_rules_keep_the_previously_up_guard():
    """The two filters answer different questions and neither replaces the
    other: `max_over_time` excludes a port that was never in service, the flag
    excludes one taken out of service on purpose. Dropping the first would
    start alerting on every never-cabled port of an unconfigured deployment."""
    rules = fabric_rules()
    for name, series in (
        ("NetworkLinkDown", "sparkdash_network_up"),
        ("RdmaPortDown", "sparkdash_rdma_port_active"),
    ):
        assert f"max_over_time({series}[7d]) == 1" in rules[name]


def test_memory_alerting_asks_whether_the_use_is_explained():
    """`MemoryNearlyFull` was a LEVEL rule and could not be cleared on a node
    doing its job: two cluster nodes hold one 193 GiB model and sit at ~87%
    forever. It fired on the nodes whose memory was most accounted for and
    stayed quiet on the one with three times the unexplained footprint.

    Its replacement subtracts resident model weights, so "full of weights" is
    silent and "full of something nobody can name" is not.
    """
    rules = fabric_rules()
    assert "MemoryNearlyFull" not in rules, (
        "the level rule is back; it cannot be cleared on a node full of weights"
    )
    expr = rules["UnexplainedMemoryUse"]
    assert "sparkdash_gpu_process_memory_bytes" in expr, "model weights are not subtracted"
    assert "or (0 * sum by (node) (sparkdash_gpu_utilization_percent))" in expr, (
        "the fallback must be a WITNESS that the GPU collector ran -- a bare "
        "`or 0` makes missing attribution look like unexplained memory, which "
        "inflated the observed peak from 25.8% to 39.4%"
    )
    assert "runtime=~" not in expr, (
        "the subtraction is filtered by runtime again. EXPLAINED MEANS "
        "ATTRIBUTED, not 'is a model': crediting only LLM runtimes made every "
        "other named GPU workload count as unexplained. sparky runs ComfyUI, "
        "and at 33 GiB resident the rule read 49.5% and fired 93 times in 7 "
        "days about memory the dashboard names two cards down. Corrected "
        "2026-08-28; Z3's 40% threshold needed no change, which is how we know "
        "it was the expression that had drifted."
    )


def test_harm_alerts_survive_alongside_it():
    """Headroom and harm are different questions. Replacing the level rule must
    not leave actual memory pressure uncovered."""
    rules = fabric_rules()
    for name in ("MemoryPressureHigh", "MemoryPressureCritical", "SwapThrashing"):
        assert name in rules, f"{name} has gone missing"
