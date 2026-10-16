"""What to set up, in order (roadmap BE1).

The checklist the onboarding guide draws, worked out from the system as it
is rather than from what a reader has clicked: a step is done when the thing
it asks for is working. Required first, then recommended, then optional,
each saying why it matters and where it is set.

States: `done`; `attention` (set up, and something is wrong); `todo` (not
set up); `info` (nothing to check, worth knowing).
"""

from __future__ import annotations

REPO = "https://github.com/anakronox/spark-dash"
DOCS = f"{REPO}/blob/main/docs/deployment.md"
NODES_DOC = f"{REPO}#2-each-gb10-node"
NTFY_DOC = f"{REPO}/blob/main/central/README.md#notifications-ntfy"


def _item(id_: str, level: str, title: str, state: str, detail: str, *, pane: str | None = None,
          doc: str | None = None) -> dict:
    return {"id": id_, "level": level, "title": title, "state": state, "detail": detail,
            "pane": pane, "doc": doc}


def checklist(*, nodes: list, prom_ok: bool, data_age_s: float | None, targets_down: int,
              am_ok: bool, routing: dict | None, cluster_file: bool, fleet: dict, gateway_on: bool,
              assistant_on: bool, mcp_on: bool) -> dict:
    items: list[dict] = []
    up = [n for n in nodes if n.up]

    # --- required ---------------------------------------------------------
    if not nodes:
        items.append(_item("nodes", "required", "Add your nodes", "todo",
                           "No nodes are configured. Add each GB10 under Nodes, then start the agent on it.",
                           pane="nodes", doc=NODES_DOC))
    elif len(up) < len(nodes):
        down = ", ".join(n.node_id for n in nodes if not n.up)
        items.append(_item("nodes", "required", "Every node reporting", "attention",
                           f"{len(up)} of {len(nodes)} reporting; not answering: {down}. Check the agent "
                           "is running there and port 9500 is reachable.", pane="nodes"))
    else:
        items.append(_item("nodes", "required", "Every node reporting", "done",
                           f"All {len(nodes)} nodes' agents answer.", pane="nodes"))

    if not prom_ok:
        items.append(_item("prometheus", "required", "Prometheus recording history", "attention",
                           "Prometheus is not answering, so there is no history, no alerting and no "
                           "agent tools. Check the prometheus container on the monitoring host."))
    elif data_age_s is not None and data_age_s > 120:
        items.append(_item("prometheus", "required", "Prometheus recording history", "attention",
                           f"The newest sample is {data_age_s / 60:.0f} minutes old: Prometheus is up but "
                           "not scraping. Check its targets."))
    elif targets_down:
        items.append(_item("prometheus", "required", "Prometheus recording history", "attention",
                           f"{targets_down} scrape target{'s are' if targets_down != 1 else ' is'} down; "
                           "their charts have gaps."))
    else:
        items.append(_item("prometheus", "required", "Prometheus recording history", "done",
                           "Scraping every target; history and alert rules are working."))

    if not am_ok:
        items.append(_item("alerting", "required", "Alerts reach someone", "attention",
                           "Alertmanager is not answering, so an alert would notify no one.",
                           pane="notifications", doc=NTFY_DOC))
    else:
        default = (routing or {}).get("default")
        channels = ((routing or {}).get("receivers") or {}).get(default) or []
        if routing is None:
            items.append(_item("alerting", "required", "Alerts reach someone", "attention",
                               "Alertmanager answers, but its configuration could not be read."))
        elif not channels:
            items.append(_item("alerting", "required", "Alerts reach someone", "todo",
                               f"Alerts go to the receiver '{default}', which has no channel: nobody is "
                               "notified. Give it one (ntfy needs no account).", pane="notifications", doc=NTFY_DOC))
        else:
            items.append(_item("alerting", "required", "Alerts reach someone", "done",
                               f"Alerts go to '{default}' by {', '.join(channels)}.", pane="notifications"))

    # --- recommended ------------------------------------------------------
    fallback = [n.node_id for n in up if n.temp_bands is not None
                and "fallback" in (n.temp_bands.gpu_source, n.temp_bands.cpu_source)]
    if up:
        items.append(_item(
            "bands", "recommended", "Thermal limits from each node's own hardware",
            "attention" if fallback else "done",
            (f"Using generic limits on {', '.join(fallback)}: the agent could not read the hardware's own, "
             "so temperature alerts may fire too early or too late. Run the agent as the README describes.")
            if fallback else "Every node's warning and critical temperatures come from its own hardware."))
    items.append(_item(
        "cluster_file", "recommended", "Nodes kept in cluster.yml", "done" if cluster_file else "todo",
        "Nodes, engines and integrations are edited here and saved to cluster.yml."
        if cluster_file else
        "This install lists its nodes in SPARK_NODES, so Settings cannot change them and the "
        "integrations below cannot be switched on. Move them into cluster.yml.", pane="nodes"))
    clusters = sorted({n.cluster for n in nodes if getattr(n, "cluster", None)})
    items.append(_item(
        "clusters", "recommended", "Name the nodes that pool together", "info",
        (f"Clusters: {', '.join(clusters)}. " if clusters else "") +
        "Nodes that serve one model together belong to one cluster, so the dashboard reads them as "
        "one workload and updates them together.", pane="nodes"))

    # --- optional ---------------------------------------------------------
    if fleet.get("capability") is not None:
        items.append(_item(
            "updates", "optional", "DGX OS updates", "done" if fleet.get("capability") and fleet.get("enabled")
            else "todo",
            "Checks each Spark for DGX OS and firmware updates and runs them from the dashboard. Needs an "
            "SSH key and a login on each Spark.", pane="updates",
            doc="https://github.com/anakronox/spark-dash/blob/main/docs/fleet-updates-setup.md"))
    items.append(_item("clients", "optional", "Client stats", "done" if gateway_on else "todo",
                       "Who calls which model, how often and with how many tokens. Needs a LiteLLM gateway "
                       "in front of the engines.", pane="clients", doc=f"{DOCS}#client-stats--optional"))
    items.append(_item("assistant", "optional", "Ask the dashboard", "done" if assistant_on else "todo",
                       "Questions about the cluster answered by a model you run.", pane="assistant",
                       doc=f"{DOCS}#asking-the-dashboard--optional"))
    items.append(_item("mcp", "optional", "Agent tools", "done" if mcp_on else "todo",
                       "Lets Claude Code, Hermes and other agents query the dashboard over MCP.", pane="mcp",
                       doc=f"{DOCS}#tools-for-agents-mcp--optional"))

    required = [i for i in items if i["level"] == "required"]
    return {
        "items": items,
        "required_done": sum(i["state"] == "done" for i in required),
        "required_total": len(required),
    }
