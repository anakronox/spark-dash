"""The gauges' Prometheus readings (roadmap BA2), with Prometheus stubbed."""

import asyncio

from spark_dash_backend.latest import LATEST_METRICS, SAMPLED_AT, latest
from spark_dash_backend.prometheus import HISTORY_QUERIES, PrometheusError, Series


class Prom:
    def __init__(self, fail: set[str] = frozenset()):
        self.asked: list[str] = []
        self.fail = fail

    async def query(self, expr):
        self.asked.append(expr)
        if expr == SAMPLED_AT:
            return [Series({"node": "sparky"}, [(100.0, 1700000000.0)])]
        if any(k in expr for k in self.fail):
            raise PrometheusError("boom")
        return [
            Series({"node": "sparky"}, [(100.0, 2850.0)]),
            Series({"node": "sparkjr"}, [(100.0, float("nan"))]),
            Series({}, [(100.0, 1.0)]),
        ]


def test_every_served_metric_is_a_named_chart_query():
    """A gauge reads the same expression as the line beside it."""
    assert LATEST_METRICS <= HISTORY_QUERIES.keys()


def test_values_per_node_and_when_each_was_sampled():
    out = asyncio.run(latest(Prom(), ["cpu_clock"]))
    # NaN and unlabelled series are dropped, not passed on as numbers.
    assert out["values"] == {"cpu_clock": {"sparky": 2850.0}}
    assert out["sampled_at"] == {"sparky": 1700000000.0}


def test_the_rate_window_is_filled_in():
    prom = Prom()
    asyncio.run(latest(prom, ["swap_io", "disk_busy"]))
    assert all("{window}" not in e for e in prom.asked)
    assert any("[1m]" in e for e in prom.asked)


def test_one_failing_query_leaves_the_others():
    out = asyncio.run(latest(Prom(fail={"node_disk_io_time"}), ["cpu_clock", "disk_busy"]))
    assert set(out["values"]) == {"cpu_clock"}


def test_only_the_menu_is_queried():
    prom = Prom()
    asyncio.run(latest(prom, ["gpu_utilization", "cpu_clock", "cpu_clock"]))
    # one metric query, once, plus the sample-time query
    assert len(prom.asked) == 2
