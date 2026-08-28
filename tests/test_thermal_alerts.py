"""The temperature warnings must stay smoothed, and must not print a fraction
as a temperature.

Both rules were rewritten on 2026-08-28 after measuring that neither could
fire. In seven days `CpuTemperatureHigh` reached `pending` 463 times and fired
0; `GpuTemperatureHigh` 891 and 0. The cause was not the threshold: in sparky's
hottest 40 minutes the CPU sat above its band 51% of the time while the longest
CONTINUOUS run was 120 seconds, against a 10 minute hold.

Two things are guarded here, and both are ways the fix silently comes undone.

**The naive form looks better.** `sparkdash_cpu_temperature_celsius >= on(node)
sparkdash_cpu_temp_warning_celsius` reads more clearly than a subquery and an
`avg_over_time`, so it is exactly what a later simplification would restore —
reinstating a rule that cannot fire, with no test failing and no symptom beyond
silence.

**Changing the expression changed what `$value` means.** It is now a fraction of
a window, not a degree count, and the original annotation would have rendered a
notification reading "CPU on sparky at 1C". That was caught before shipping,
which is not a reason to leave it uncaught next time.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ALERTS_YML = Path(__file__).resolve().parent.parent / "central" / "config" / "alerts.yml"

#: alert -> minimum hold. Calibrated against 7 days of real data: at these
#: values the CPU rule fires ~2x/week and the GPU ~3x/week, both only on sparky.
#: The GPU's is longer because it is meant to run hot and has `GpuThrottled`
#: covering the consequence at critical severity.
SMOOTHED = {"CpuTemperatureHigh": 600, "GpuTemperatureHigh": 900}


def rules() -> dict[str, dict]:
    doc = yaml.safe_load(ALERTS_YML.read_text())
    return {
        r["alert"]: r
        for g in doc["groups"]
        for r in g["rules"]
        if "alert" in r
    }


@pytest.fixture(scope="module")
def by_name() -> dict[str, dict]:
    return rules()


@pytest.mark.parametrize("name", sorted(SMOOTHED))
def test_the_warning_still_smooths_its_input(name, by_name):
    """A bare comparison here is a rule that cannot fire against a sensor that
    oscillates across its own band."""
    expr = " ".join(by_name[name]["expr"].split())
    assert "avg_over_time" in expr, f"{name} lost its smoothing window"
    assert "bool" in expr, (
        f"{name} needs `>= bool` — without it the comparison filters series "
        "instead of yielding the 1/0 the average is taken over"
    )
    assert "[10m:" in expr, f"{name} lost the 10m window the annotation describes"


@pytest.mark.parametrize("name", sorted(SMOOTHED))
def test_the_hold_matches_what_was_calibrated(name, by_name):
    """Shortening these was the tempting fix and is the wrong one: the observed
    runs clear a 2 minute hold, so a short one fires on single spikes."""
    got = by_name[name]["for"]
    seconds = {"m": 60, "h": 3600, "s": 1}[got[-1]] * int(got[:-1])
    assert seconds >= SMOOTHED[name], f"{name} holds {got}, calibrated for >= {SMOOTHED[name]}s"


@pytest.mark.parametrize("name", sorted(SMOOTHED))
def test_the_summary_does_not_print_a_fraction_as_a_temperature(name, by_name):
    """`$value` is a fraction of a window now. The pre-existing wording would
    have sent "CPU on sparky at 1C" to a phone."""
    summary = by_name[name]["annotations"]["summary"]
    if "$value" in summary:
        assert "humanizePercentage" in summary, (
            f"{name} prints $value without formatting it as the fraction it now is"
        )
        assert "}}C" not in summary, f"{name} still labels $value as degrees"


def test_the_criticals_were_deliberately_left_alone(by_name):
    """They also go pending and never fire, and for them that is CORRECT: the
    GPU touches its 86C slowdown for a few 15s samples and backs off, which is
    the transient a hold exists to filter.

    Pinned so that a later pass sweeping "rules that never fire" does not treat
    the two cases as one problem."""
    for name in ("CpuTemperatureCritical", "GpuTemperatureCritical"):
        expr = " ".join(by_name[name]["expr"].split())
        assert "avg_over_time" not in expr, (
            f"{name} was smoothed — see the note: its silence is the hold working, "
            "not the rule failing"
        )
