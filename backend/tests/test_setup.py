"""The onboarding checklist (roadmap BE1): each step's state from the system."""

from __future__ import annotations

from types import SimpleNamespace as NS

from spark_dash_backend import setup

BANDS = NS(gpu_source="nvml", cpu_source="thermal-zone")


def node(id_, up=True, bands=BANDS, cluster=None):
    return NS(node_id=id_, up=up, temp_bands=bands, cluster=cluster)


def run(**over):
    base = dict(nodes=[node("sparky"), node("sparketa")], prom_ok=True, data_age_s=3.0, targets_down=0,
                am_ok=True, routing={"default": "ntfy", "receivers": {"ntfy": ["webhook"]}},
                cluster_file=True, fleet={"capability": False, "enabled": False}, gateway_on=False,
                assistant_on=False, mcp_on=False)
    base.update(over)
    out = setup.checklist(**base)
    return out, {i["id"]: i for i in out["items"]}


def test_a_working_install_has_every_required_step_done():
    out, by = run()
    assert (out["required_done"], out["required_total"]) == (3, 3)
    assert [i["level"] for i in out["items"]][:3] == ["required"] * 3
    assert by["alerting"]["detail"] == "Alerts go to 'ntfy' by webhook."


def test_required_first_then_recommended_then_optional():
    out, _ = run()
    order = {"required": 0, "recommended": 1, "optional": 2}
    levels = [order[i["level"]] for i in out["items"]]
    assert levels == sorted(levels)


def test_a_node_down_is_named():
    out, by = run(nodes=[node("sparky"), node("sparkjr", up=False)])
    assert by["nodes"]["state"] == "attention" and "sparkjr" in by["nodes"]["detail"]
    assert out["required_done"] == 2


def test_a_receiver_with_no_channel_notifies_nobody():
    _, by = run(routing={"default": "null", "receivers": {"null": []}})
    assert by["alerting"]["state"] == "todo" and "nobody is notified" in by["alerting"]["detail"]


def test_alertmanager_down_and_prometheus_stale():
    _, by = run(am_ok=False, data_age_s=900)
    assert by["alerting"]["state"] == "attention"
    assert by["prometheus"]["state"] == "attention" and "15 minutes" in by["prometheus"]["detail"]


def test_fallback_thermal_limits_are_flagged():
    _, by = run(nodes=[node("sparky", bands=NS(gpu_source="fallback", cpu_source="thermal-zone"))])
    assert by["bands"]["state"] == "attention" and "sparky" in by["bands"]["detail"]


def test_no_nodes_and_no_cluster_file():
    out, by = run(nodes=[], cluster_file=False)
    assert by["nodes"]["state"] == "todo" and by["cluster_file"]["state"] == "todo"
    assert "bands" not in by


def test_optional_steps_reflect_what_is_on_and_point_at_their_pane():
    _, by = run(gateway_on=True, mcp_on=True, fleet={"capability": True, "enabled": True})
    assert by["clients"]["state"] == "done" and by["mcp"]["state"] == "done"
    assert by["updates"]["state"] == "done" and by["assistant"]["state"] == "todo"
    assert {by[k]["pane"] for k in ("clients", "assistant", "mcp", "updates")} == {"clients", "assistant", "mcp", "updates"}
