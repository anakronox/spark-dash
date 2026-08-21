"""One engine list, checked across the four places that have to agree.

Adding an inference engine touches Python (the collector specs and the
snapshot model), TypeScript (the dashboard's own copy), Prometheus (a scrape
job and a target file per engine) and the alert rules (which split targets
into infrastructure and inference by job name). Those are four languages in
four directories, which is exactly the shape that drifts — and each way of
drifting is silent:

  - a spec with no `Runtimes` field: the agent raises only on a node running
    that engine
  - an engine with no scrape job: no history at all, while the live view looks
    complete
  - an engine missing from the alert rules' job lists: either it never alerts
    on a failed scrape, or it alerts under the infrastructure rule that never
    ages out
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from spark_dash_agent.collectors.engine import SPECS
from spark_dash_common.models import ENGINE_RUNTIMES, Runtimes

ROOT = Path(__file__).resolve().parent.parent
TYPES_TS = ROOT / "frontend" / "src" / "lib" / "types.ts"
PROMETHEUS_YML = ROOT / "central" / "config" / "prometheus.yml"
ALERTS_YML = ROOT / "central" / "config" / "alerts.yml"


def test_every_engine_has_a_collector_spec():
    """A field with no spec is an engine the agent never scrapes, reported as
    an empty list rather than as a gap."""
    assert set(ENGINE_RUNTIMES) == set(SPECS)


def test_every_spec_lands_in_the_snapshot():
    assert set(SPECS) <= set(Runtimes().engines)


def test_the_frontend_knows_the_same_engines():
    src = TYPES_TS.read_text()
    match = re.search(r"export const ENGINE_RUNTIMES = \[(.*?)\]", src, re.S)
    assert match, "ENGINE_RUNTIMES not found in types.ts"
    assert set(re.findall(r"'([a-z0-9_.]+)'", match.group(1))) == set(ENGINE_RUNTIMES)


def _scrape_jobs() -> dict[str, list[str]]:
    config = yaml.safe_load(PROMETHEUS_YML.read_text())
    return {
        job["job_name"]: [
            f for sd in job.get("file_sd_configs", []) for f in sd.get("files", [])
        ]
        for job in config["scrape_configs"]
    }


def test_every_engine_has_a_scrape_job_reading_its_target_file():
    """The job name IS the runtime name — the retire endpoint and the
    dashboard's retire button both rely on that, and the target file is what
    the backend renders per engine."""
    jobs = _scrape_jobs()
    for runtime in ENGINE_RUNTIMES:
        assert runtime in jobs, f"no Prometheus scrape job for {runtime}"
        assert jobs[runtime] == [f"/etc/prometheus/targets/generated/{runtime}.yml"]


def _alert_expr(name: str) -> str:
    rules = yaml.safe_load(ALERTS_YML.read_text())
    for group in rules["groups"]:
        for rule in group.get("rules", []):
            if rule.get("alert") == name:
                return rule["expr"]
    raise AssertionError(f"alert {name!r} not found")


def test_the_two_scrape_alerts_partition_the_jobs_by_engine():
    """They must stay complements. A job in neither would never alert on a
    failed scrape at all; a job in both would alert twice under rules that age
    out differently — the infrastructure one never does, which is the whole
    reason inference targets are separated from it."""
    infra = _alert_expr("PrometheusTargetScrapeFailing")
    inference = _alert_expr("InferenceTargetScrapeFailing")

    excluded = re.search(r'up\{job!~"([^"]+)"\}', infra)
    assert excluded, "infrastructure rule no longer excludes jobs by name"
    assert set(excluded.group(1).split("|")) == set(ENGINE_RUNTIMES)

    for match in re.findall(r'up\{job=~"([^"]+)"\}', inference):
        assert set(match.split("|")) == set(ENGINE_RUNTIMES)
