"""The single-host variants stay in step with the files they were forked from.

Single-host mode duplicates exactly two things: the Prometheus scrape config
and one alert rule. Everything else is inherited — compose.single-host.yaml is
an overlay, and alerts.yml is shared through the same mount. Two copies of
anything is a drift hazard, so the drift is checked rather than hoped about.

The failure this prevents is quiet in both directions. A scrape job added to
prometheus.yml and not its sibling means a single-host install silently stops
collecting something. A rule edited in one file and not the other means the two
layouts alert differently for reasons nobody chose.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

CONFIG = Path(__file__).resolve().parent.parent / "central" / "config"
COMPOSE = Path(__file__).resolve().parent.parent / "central" / "compose.single-host.yaml"

MULTI = CONFIG / "prometheus.yml"
SINGLE = CONFIG / "prometheus.single-host.yml"

#: The job that legitimately differs, and the only one. On one box this
#: exporter would invent a node that does not exist, so it is not started and
#: not scraped.
DIVERGENT_JOB = "node-exporter-central"


def jobs(path: Path) -> dict[str, dict]:
    conf = yaml.safe_load(path.read_text())
    return {j["job_name"]: j for j in conf["scrape_configs"]}


def test_the_only_job_that_differs_is_the_central_exporter():
    """Everything else must be collected identically. A job present in one file
    and not the other is a single-host install quietly missing data."""
    multi, single = jobs(MULTI), jobs(SINGLE)
    assert set(multi) - set(single) == {DIVERGENT_JOB}
    assert not set(single) - set(multi), (
        f"single-host scrapes jobs the multi-host layout does not: "
        f"{sorted(set(single) - set(multi))}"
    )


@pytest.mark.parametrize("job", sorted(set(jobs(SINGLE))))
def test_shared_jobs_are_configured_identically(job):
    """Same targets, same intervals, same relabelling. If a job needs to differ
    it should be as loud as the central exporter is, not a silent divergence."""
    assert jobs(MULTI)[job] == jobs(SINGLE)[job], (
        f"{job} is configured differently between layouts"
    )


def test_each_config_loads_the_matching_storage_rules():
    """The storage rule is host-pinned, so the wrong pairing is a real bug:
    the single-host rule on a multi-host Prometheus raises `found duplicate
    series for the match group {}` and stops evaluating — verified against a
    live multi-host instance."""
    for path, expected in (
        (MULTI, "alerts-storage.yml"),
        (SINGLE, "alerts-storage.single-host.yml"),
    ):
        files = [f.split("/")[-1] for f in yaml.safe_load(path.read_text())["rule_files"]]
        assert "alerts.yml" in files, f"{path.name} does not load the shared rules"
        assert expected in files, f"{path.name} should load {expected}, got {files}"
        wrong = {"alerts-storage.yml", "alerts-storage.single-host.yml"} - {expected}
        assert not (wrong & set(files)), f"{path.name} loads both storage variants"


def test_the_storage_rule_differs_only_in_its_host_pin():
    """One rule, two hosts to pin to. Anything else diverging means the two
    layouts have started alerting differently for reasons nobody chose."""
    def rule(name: str) -> dict:
        return yaml.safe_load((CONFIG / name).read_text())["groups"][0]["rules"][0]

    multi, single = rule("alerts-storage.yml"), rule("alerts-storage.single-host.yml")
    assert multi["alert"] == single["alert"] == "PrometheusStorageFillingUp"
    assert multi["for"] == single["for"]
    assert multi["annotations"] == single["annotations"]

    normalised = multi["expr"].replace('job="node-exporter-central", ', "")
    assert normalised == single["expr"], (
        "the storage rules differ by more than the job pin:\n"
        f"  multi : {multi['expr']}\n  single: {single['expr']}"
    )


def test_the_shared_rules_carry_no_host_pin():
    """alerts.yml is loaded by BOTH layouts, so a rule in it that names
    `node-exporter-central` would be broken on single-host — which has no such
    job."""
    body = (CONFIG / "alerts.yml").read_text()
    offenders = [
        line.strip()
        for line in body.splitlines()
        if DIVERGENT_JOB in line and not line.strip().startswith("#")
    ]
    assert not offenders, (
        f"shared rules reference {DIVERGENT_JOB}, which single-host does not run: "
        f"{offenders}"
    )


def test_the_overlay_only_removes_and_repoints():
    """An overlay that redefined ports, volumes or images would be a second
    stack wearing a hat, and the two would drift. It may drop the exporter and
    point Prometheus at the other config; nothing else."""
    conf = yaml.safe_load(COMPOSE.read_text())
    assert set(conf["services"]) == {"node-exporter", "prometheus"}
    assert conf["services"]["node-exporter"] == {"deploy": {"replicas": 0}}

    prom = conf["services"]["prometheus"]
    assert set(prom) == {"command"}, f"overlay changes more than the command: {set(prom)}"
    assert any("prometheus.single-host.yml" in c for c in prom["command"])


def test_the_overlay_keeps_prometheus_flags_in_step():
    """The command is replaced wholesale, not merged, so a flag added to
    compose.yaml is silently lost here. That is how retention or the lifecycle
    endpoint goes missing on one layout only."""
    base = yaml.safe_load((COMPOSE.parent / "compose.yaml").read_text())
    base_flags = {
        c.split("=")[0]
        for c in base["services"]["prometheus"]["command"]
    }
    overlay_flags = {
        c.split("=")[0]
        for c in yaml.safe_load(COMPOSE.read_text())["services"]["prometheus"]["command"]
    }
    assert base_flags == overlay_flags, (
        f"flags differ between layouts: only in base {base_flags - overlay_flags}, "
        f"only in overlay {overlay_flags - base_flags}"
    )
