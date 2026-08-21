"""Straggler rules compare cluster members against each other, and stay honest.

These are the only rules in the file that ask a relative question — every other
alert judges a node on its own terms. That makes them easy to get subtly wrong
in ways nothing else would catch, and each way is silent:

  - comparing instantaneous values instead of averaged ones fires constantly on
    a healthy pair (measured spread: -117..+110 MHz, -13..+15 C)
  - comparing against the cluster MEAN halves the apparent gap at n=2, so a
    threshold means double what it reads
  - forgetting the multi-node guard makes a standalone node its own max, so the
    deviation is always exactly zero and the rule is inert while looking live
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ALERTS_YML = Path(__file__).resolve().parent.parent / "central" / "config" / "alerts.yml"

CLUSTER_ALERTS = ("ClusterNodeClockLagging", "ClusterNodeRunningHot")


@pytest.fixture(scope="module")
def rules() -> dict[str, dict]:
    groups = yaml.safe_load(ALERTS_YML.read_text())["groups"]
    return {
        r["alert"]: r
        for g in groups
        for r in g.get("rules", [])
        if "alert" in r
    }


@pytest.mark.parametrize("name", CLUSTER_ALERTS)
def test_the_comparison_is_averaged_not_instantaneous(rules, name):
    """A healthy pair swings -117..+110 MHz and -13..+15 C sample to sample.
    Averaged over 15 minutes the same pair sits inside ~5 MHz and ~1.2 C, which
    is the only reason a tight threshold can be quiet."""
    expr = rules[name]["expr"]
    assert "avg_over_time(" in expr, f"{name} compares raw samples"
    assert "[15m]" in expr, f"{name} does not use the calibrated window"


@pytest.mark.parametrize("name", CLUSTER_ALERTS)
def test_it_only_evaluates_inside_a_real_cluster(rules, name):
    """A standalone node is its own max and its own min, so every deviation is
    exactly zero -- the rule would look live and be inert. The label filter
    also keeps unclustered nodes out entirely."""
    expr = rules[name]["expr"]
    assert 'cluster=~".+"' in expr, f"{name} does not restrict to clustered nodes"
    assert "count by (cluster) (sparkdash_node_up" in expr, (
        f"{name} does not require 2+ members"
    )


@pytest.mark.parametrize("name", CLUSTER_ALERTS)
def test_it_compares_against_the_best_peer_not_the_mean(rules, name):
    """With two nodes the mean sits exactly between them, so each deviates by
    HALF the real gap and a 50 MHz threshold would mean a 100 MHz gap. Against
    max/min the number is the full gap at any cluster size."""
    expr = rules[name]["expr"]
    assert re.search(r"(max|min) by \(cluster\)", expr), (
        f"{name} does not compare against a cluster extreme"
    )
    assert "avg by (cluster)" not in expr, (
        f"{name} compares against the mean, which halves the gap at n=2"
    )


def test_the_clock_rule_only_fires_while_the_cluster_is_working():
    """At idle, clocks drop independently and the comparison means nothing.
    Only ~15% of a measured 48h window had the cluster busy at all."""
    groups = yaml.safe_load(ALERTS_YML.read_text())["groups"]
    expr = next(r["expr"] for g in groups for r in g.get("rules", [])
                if r.get("alert") == "ClusterNodeClockLagging")
    assert "sparkdash_gpu_utilization_percent" in expr, (
        "the clock rule is not gated on the cluster doing work"
    )


def test_power_is_not_alerted_on():
    """n=2 cannot vote, so every cluster rule must be DIRECTIONAL -- the
    metric's own semantics have to name the bad side. Power does not: lower can
    mean stalled or idle, higher can mean working hard or leaking."""
    groups = yaml.safe_load(ALERTS_YML.read_text())["groups"]
    cluster_group = next(g for g in groups if g["name"] == "cluster")
    for rule in cluster_group["rules"]:
        assert "power_watts" not in rule["expr"], (
            f"{rule['alert']} alerts on power, which has no bad direction"
        )


def test_the_thermal_rule_is_distinct_from_the_absolute_one():
    """GpuTemperatureHigh asks whether a node is near its own limit;
    ClusterNodeRunningHot asks whether it is out of step with peers doing
    identical work, and fires long before the absolute threshold. Losing either
    loses a real signal."""
    groups = yaml.safe_load(ALERTS_YML.read_text())["groups"]
    names = {r["alert"] for g in groups for r in g.get("rules", []) if "alert" in r}
    assert {"GpuTemperatureHigh", "ClusterNodeRunningHot"} <= names
