"""The disk ladder: three fixed breakpoints, exactly one of them true.

Replaced `NodeDiskFillingUp` on 2026-08-28. That rule projected the last 6h of
growth forward a week and fired on sparky at **66.7% full with 1.2 TB free** —
arithmetically correct and practically wrong, because disk here staircases
rather than slopes. Measured over 24h, sparky sat flat for seven and a half
hours overnight, then stepped down 4, 25, 4, 27 and 23 GiB as models landed. A
6h window catching two of those steps reads ~9.3 GiB/h; the real 24h average
was 3.5, or 14.5 days of headroom.

Widening the window to 24h was tried and works. The objection that settled it
was different: the approach guesses at intent the operator already has. Someone
pulling a 25 GiB model knows they are pulling it.

Two properties are guarded, and the second is the one a later edit breaks
without noticing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ALERTS = Path(__file__).resolve().parent.parent / "central" / "config" / "alerts.yml"

#: alert -> (upper bound on free ratio, inclusive lower bound or None)
LADDER = {
    "NodeDiskFilling": (0.15, 0.10),
    "NodeDiskWarning": (0.10, 0.05),
    "NodeDiskLow": (0.05, None),
}


def rules() -> dict[str, dict]:
    doc = yaml.safe_load(ALERTS.read_text())
    return {r["alert"]: r for g in doc["groups"] for r in g["rules"] if "alert" in r}


def test_the_projection_rule_is_gone():
    assert "NodeDiskFillingUp" not in rules(), (
        "predict_linear is back. It fired at 66.7% full because disk staircases "
        "rather than slopes; see this module's docstring before reinstating it."
    )


@pytest.mark.parametrize("name", sorted(LADDER))
def test_each_tier_is_bounded_on_both_sides(name):
    """The upper tiers must be bounded BELOW as well as above.

    Without the lower bound every tier is true at 97% full, and a filling disk
    raises three alerts saying the same thing at increasing volume. `NodeDiskLow`
    is the exception: it is the top of the ladder and has nothing above it.
    """
    expr = " ".join(rules()[name]["expr"].split())
    upper, lower = LADDER[name]
    assert f"< {upper}" in expr, f"{name} lost its upper bound"
    if lower is None:
        assert ">=" not in expr, "the critical tier must not be bounded below"
    else:
        assert f">= {lower}" in expr, (
            f"{name} is unbounded below, so it also fires inside the tier above it"
        )


def test_the_bands_leave_no_gap():
    """A boundary value must land in exactly one tier.

    Inclusive lower bounds are deliberate: with strict ones a disk sitting on
    exactly 0.10 free would match NEITHER the 85 nor the 90 tier and go
    unreported. A value on a boundary belongs to the lower-severity tier, which
    is the conservative direction.
    """
    # Only the INTERIOR boundaries. The outermost upper bound (0.15) is the top
    # of the ladder: above it nothing fires, which is the point of a ladder.
    lowers = {lower for _, lower in LADDER.values() if lower is not None}
    for b in sorted(lowers):
        matched = [
            n
            for n, (upper, lower) in LADDER.items()
            if b < upper and (lower is None or b >= lower)
        ]
        assert len(matched) == 1, f"free ratio {b} matches {matched}, expected exactly one"


def test_severities_escalate():
    r = rules()
    assert r["NodeDiskFilling"]["labels"]["severity"] == "warning"
    assert r["NodeDiskWarning"]["labels"]["severity"] == "warning"
    assert r["NodeDiskLow"]["labels"]["severity"] == "critical"
