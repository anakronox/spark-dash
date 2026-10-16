"""What an agent reads and how it reports (roadmap BB15, BB16).

RESOURCES are reference an agent can open instead of guessing what a series
or a reading means: the GB10 notes, a metrics guide generated from the same
table the tools use (so it cannot drift from them), and how to investigate.

PROMPTS are report templates any MCP client can offer, so Hermes, Claude Code
and Cursor produce the same shape of report from the same tools.

Kept in the backend package, not docs/, because the image ships the package
and not the repository.
"""

from __future__ import annotations

from mcp.server.mcpserver.resources.types import TextResource

from .prometheus import HISTORY_QUERIES

GB10 = """\
# NVIDIA GB10 (DGX Spark, ASUS GX10): what the readings mean

## Memory is one pool
The CPU and GPU share one coherent pool of about 121.7 GiB (LPDDR5x). There is
no separate VRAM. So:
- Memory pressure on a node affects the GPU and the CPU at once.
- Every GPU workload competes with every other for the same pool: a ComfyUI
  job and a llama.cpp model take from one budget. `gpu_workload_memory_bytes`
  says who holds what.
- NVML's own memory figure is wrong on this hardware (it reports roughly the
  whole pool whatever is allocated). The dashboard's memory figures come from
  the kernel (`MemTotal - MemAvailable`), not NVML. Ignore any tool or
  exporter that reports GPU memory from NVML on GB10.

## Temperature limits come from each node's own hardware
- GPU limits are read from NVML's thresholds, not from nvidia-smi, which
  reports N/A for them. On the GX10 measured here the GPU slows itself at
  86 °C and shuts down at 90 °C.
- The SoC thermal zones (`acpitz`, the "package" group) trip at 104.8 °C,
  where the kernel powers the machine off.
- Each node's warning and critical bands are in `cluster_now`
  (`temperature_bands_c`); the alert rules use the same bands. A node can
  differ from another, so read the bands rather than assuming them.
- The hottest sensor is usually not the one closest to its limit: an NVMe at
  60 °C rated to 84.85 has less margin than a NIC at 60 rated to 105.
  `temperature_headroom` ranks them on one scale.
- Never average temperatures to judge risk: risk is the hottest point. The
  dashboard's averages are an overview only, and its colours always come
  from the sensor closest to its limit.

## Clocks
NVML reports a 3003 MHz maximum that this part never reaches. Under load the
GPU holds a steady clock; a sustained drop under load, with temperature near
its band, is throttling. Compare a node with itself over time
(`metric_history gpu_clock`), not with the reported maximum.

## Power
`gpu_power` is the GPU alone. NVML on GB10 reports no whole-module power, and
there is no software figure for the node's wall power.
"""

INVESTIGATING = """\
# Investigating with the spark-dash tools

1. **Where things stand.** `cluster_now` for each node now, `topology` for
   which nodes pool into one workload. Nodes in one cluster serving one model
   are one workload: read them together.
2. **Is the data there?** `monitoring_health` for the window first. Inside a
   gap a node's series are missing, not zero; an average over a gap is low
   for the wrong reason.
3. **What moved.** `correlate` with the node and the moment ranks every
   series by how far it left its baseline. `compare` sets two windows side by
   side (this week and last, before and after a change).
4. **Who was using the GPU.** `metric_history gpu_workload_sm` and
   `gpu_workload_memory_bytes` are per workload (runtime and model), for any
   past window. `cluster_now`'s process list is only the present.
5. **Serving.** `model_activity` for loads and how long they took,
   throughput and time to first token; `latency` with a window for time to
   first token per model; `clients` for who called through the gateway.
6. **Alerting.** `alerting` for the rules, what is firing, what is silenced
   and why; `alerts` and `events` for what happened in a window.
7. **Maintenance.** `updates` for each node's DGX OS release, pending
   packages and firmware. `add_note` marks a moment on the timeline ("load
   test started") when Settings allows it, so a later `correlate` or chart
   explains the spike; notes come back from `events` as kind `note`.

For a report, the prompts `daily_report`, `incident_report` and
`capacity_check` give the shape.

## Reading the answers

- **GPU busy, no engine traffic.** If `gpu_utilization` is high while the
  engines' `tokens_per_second` and `clients` are near zero, the work is not
  going through a monitored engine or the gateway: an image generator
  (ComfyUI), a training run, a notebook. `gpu_workload_sm` names it.
- **Several ComfyUI instances aggregate as one** `comfyui` workload: their
  command lines are identical, so nothing tells them apart.
- **llama.cpp has no time-to-first-token histogram.** Its models have a
  latency only through the gateway, when one is configured; the engines
  (vLLM, SGLang, TensorFold) report their own.
- **A p95 time to first token of exactly 300 s** is usually a client's own
  timeout, not the engine taking that long.
- **`unrecognised` in the workload series** is GPU work from a process the
  agent could not put a runtime to.
- **Load times** from `model_activity` are counted in scrape steps; each
  carries its own plus-or-minus.

Report the node, the time (UTC) and the figure behind every claim. Say when
the data cannot answer a question, and what would.
"""


def metrics_guide(metrics: dict[str, tuple[str, str]]) -> str:
    lines = [
        "# The named series",
        "",
        "Every series `metric_history`, `correlate` and `compare` read, with its unit,",
        "what it means and the PromQL behind it. `{window}` is a rate window the tools",
        "scale to the step.",
        "",
    ]
    for key, (unit, what) in metrics.items():
        lines += [f"## {key} ({unit})", what, "", f"    {HISTORY_QUERIES.get(key, '')}", ""]
    return "\n".join(lines)


def resources(metrics: dict[str, tuple[str, str]]) -> list[TextResource]:
    return [
        TextResource(uri="sparkdash://guide/gb10", name="gb10", title="GB10: what the readings mean",
                     description="Unified memory, temperature limits, clocks and power on NVIDIA GB10",
                     mime_type="text/markdown", text=GB10),
        TextResource(uri="sparkdash://guide/metrics", name="metrics", title="The named series",
                     description="Every series the tools read: unit, meaning, PromQL",
                     mime_type="text/markdown", text=metrics_guide(metrics)),
        TextResource(uri="sparkdash://guide/investigating", name="investigating",
                     title="Investigating with the spark-dash tools",
                     description="Which tool answers which question, in the order to use them",
                     mime_type="text/markdown", text=INVESTIGATING),
    ]


# --- prompts (BB15) -------------------------------------------------------------

def daily_report(day: str = "yesterday") -> str:
    return (
        f"Write a daily report for the GB10 cluster for {day} (UTC), using the spark-dash tools. "
        "First call monitoring_health for the day and say if any data is missing. Then, per node: "
        "GPU utilization, temperature and memory (min, average, peak and when), from metric_history; "
        "what used the GPU, from gpu_workload_sm and gpu_workload_memory_bytes; alerts that fired, from "
        "alerts; model loads and how long they took, from model_activity; time to first token per "
        "model, from latency with the day as the window; and who called, from clients. End with "
        "anything worth acting on, each with its node, time and figure. Keep it to one screen."
    )


def incident_report(node: str, start: str, end: str) -> str:
    return (
        f"Write an incident report for {node} between {start} and {end} (UTC), using the spark-dash "
        "tools. Check monitoring_health for the window first. Call correlate at the worst moment "
        "(find it with metric_history if it is not obvious) and compare the window with the same "
        "hours a day earlier. Say what was using the GPU from gpu_workload_sm, what alerts and events "
        "happened, and whether the other nodes saw the same thing. Structure: summary (two "
        "sentences), timeline (UTC, one line per event), likely cause with the figures behind it, "
        "what would confirm it, and what to change. Name the node, time and figure behind every "
        "claim, and say plainly where the data stops."
    )


def capacity_check() -> str:
    return (
        "Using the spark-dash tools, say where the cluster has room now and where it is tight. "
        "cluster_now for each node's free memory, GPU use and loaded models; topology for which nodes "
        "pool together; gpu_workload_memory_bytes over the last day for what holds the pool and its "
        "peaks; temperature headroom and memory pressure over the last day from metric_history. "
        "Remember GB10's memory is one pool shared by every GPU workload. Answer: which node could "
        "take another model now and roughly how much, and what is closest to a limit."
    )


PROMPTS = [
    (daily_report, "daily_report", "Daily report",
     "A day's report for the cluster: data gaps, per-node load and temperature, what used the GPU, "
     "alerts, model loads, latency, who called"),
    (incident_report, "incident_report", "Incident report",
     "What happened on one node in a window: timeline, likely cause with its figures, what to change"),
    (capacity_check, "capacity_check", "Capacity check",
     "Where the cluster has room and where it is tight, in GB10's one shared memory pool"),
]
