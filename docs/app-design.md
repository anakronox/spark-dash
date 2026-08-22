# Application Design — Backend & Frontend

Settles [open decision #1](roadmap.md#open-decisions). Covers the API surface,
the live-update contract, repo layout, and the visual design rules for panels.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Agent (per node) | **Python 3.12** | `sparkview` (our reference implementation for all GB10 logic) is Python on `nvitop`/`psutil`. UMA memory calc and PSI parsing come nearly free; rewriting in Go would mean re-deriving them by hand against `/proc` and NVML. |
| Backend | **Python 3.12 + FastAPI** | Same language as the agent, so metric models/parsing are shared code rather than duplicated. Native async fan-out across nodes, native WebSockets. |
| Frontend | **Svelte 5 + Vite + TypeScript** | Runes (`$state`/`$derived`) map cleanly onto a WebSocket pushing a snapshot every 1-2s — no virtual-DOM diffing overhead on dense tables at that cadence, and far less boilerplate than React for a solo-maintained project. |
| Charts | **uPlot** | ~40KB, purpose-built for fast time-series redraws. Recharts/Chart.js are heavier and slower under frequent updates. |
| Styling | Plain CSS with custom properties | No component library. The look is dense and information-first (see [Visual design](#visual-design)); a Material/Bootstrap kit would fight that. |

Everything containerized per [deployment.md](deployment.md).

## Repo layout

Monorepo, since the agent and backend share code:

```
spark-dash-homegrown/
├── docs/
├── common/                      # shared Python pkg: metric models, PromQL helpers,
│   └── spark_dash_common/       #   exposition-format parsing, node inventory types
├── agent/                       # spark-dash-agent: one image, per-node
│   └── collectors/              #   gpu / memory / psi / clock / llama_router
├── backend/
│   └── app/                     # FastAPI: REST (history) + WebSocket (live)
├── frontend/                    # Svelte 5 + Vite
├── scripts/                     # build-images.sh, publish-images.sh, validate-on-gx10.sh
├── node/                        # per-GX10 stack (identical on all 3)
│   └── compose.yaml             #   just this — every mount is an absolute host path
└── central/                     # Prometheus + Alertmanager + backend (Proxmox VM)
    ├── compose.yaml             #   every mount is ./something, no DATA_ROOT
    ├── config/                  #   tracked: prometheus.yml, alerts.yml, vllm-targets.yml
    └── (cluster/ prometheus/ alertmanager/ secrets/ targets/ — gitignored runtime state)
```

`common/` is installed into each image as a local path dependency (Docker build
context at repo root). Keeps the agent and backend from drifting on metric
names/shapes.

**Frontend is served by the backend container.** Vite builds to static assets
that FastAPI serves directly — one less container, no CORS config, and the
WebSocket is same-origin. Not worth a separate nginx service at this scale.

## API surface

### REST — history & inventory (Prometheus-backed)

| Endpoint | Returns |
|---|---|
| `GET /api/nodes` | Node inventory + liveness/last-seen |
| `GET /api/cluster/summary` | Aggregate GPU utilization, free capacity, total tokens/sec |
| `GET /api/models` | "What's running where": node × runtime × model × status |

A field per engine under `runtimes`, even though vLLM and SGLang carry the same
shape. The alternative — one `engines` list with a `runtime` discriminator —
would rename the `sparkdash_vllm_*` metric family that the alert rules and every
recorded series are written against. The collector is shared (one spec of metric
names per engine); the wire is per-engine. `kv_cache_pct` is null for an engine
that reports no occupancy — see [metrics](metrics.md#sglang-per-instance--same-shape-different-names).
| `GET /api/history?metric=&node=&from=&to=&step=` | Time-series for trend charts; thin PromQL wrapper |
| `GET /health` | Liveness + self-assessment, for the external UptimeKuma check |

`/health` is deliberately more than a bare `200 OK` — it reports **degraded**
when Prometheus is unreachable or the live-poller loop has stalled, so a
backend that's running but wedged is caught rather than passing a naive check.
This is what [UptimeKuma watches](deployment.md#monitoring-the-monitor--existing-uptimekuma-instance)
to close the "who monitors the monitor" gap.

### WebSocket — live view

`GET /ws/live?scope=cluster` or `?scope=node:<id>`

**Full snapshot per tick, not deltas.** A snapshot for 3 nodes is a few KB; at
1Hz that's negligible on a LAN, and it makes both sides stateless — no
resync-after-reconnect logic, no delta-application bugs. Deltas would be a
premature optimization here.

Two behaviors that matter:

- **One shared poller, not one per connection.** A single backend task polls
  each node's agent and fans the result out to all subscribers. Two browser
  tabs open must not double the polling load on the inference nodes.
- **Poll only while subscribed.** No connected clients → no tight polling loop.
  Prometheus keeps scraping on its own slower interval regardless, so history
  never has gaps; only the 1-2s live path idles.

Snapshot shape (illustrative):

```jsonc
{
  "ts": "2026-08-13T18:20:31Z",
  "nodes": [{
    "id": "gx10-1",
    "up": true,
    "gpu": {"util_pct": 87, "mem_used_bytes": 91234567890, "mem_total_bytes": 128000000000,
            "temp_c": 72, "power_w": 94, "clock_mhz": 2380, "clock_state": "PASS"},
    "psi": {"mem_state": "LOW", "some_avg10": 0.4},
    "cpu": {"util_pct": 22, "temp_c": 58},
    "processes": [{"pid": 4412, "name": "llama-server", "gpu_mem_bytes": 42000000000,
                   "runtime": "llama.cpp", "model": "qwen3-32b"}],
    "runtimes": {
      // THREE throughput fields, and only one of them is "throughput".
      // `generation_tokens_per_sec` is decode and is what every surface leads
      // with; `prompt_tokens_per_sec` is prefill, which arrives in bursts three
      // orders of magnitude larger; `tokens_per_sec` is the two added together
      // and is kept only because recorded history is written against it.
      "llama_cpp": {"loaded_models": ["qwen3-32b"], "slots_used": 2, "slots_total": 4,
                    "generation_tokens_per_sec": 41.2, "tokens_per_sec": 41.2},
      "vllm": [{"model": "llama-3.3-70b", "running": 1, "waiting": 0,
                "kv_cache_pct": 0.63, "generation_tokens_per_sec": 88.5,
                "prompt_tokens_per_sec": 0.0, "tokens_per_sec": 88.5}],
      "sglang": [{"model": "deepseek-v3", "running": 2, "waiting": 5,
                  "kv_cache_pct": null, "generation_tokens_per_sec": 137.5,
                  "prompt_tokens_per_sec": 0.0, "tokens_per_sec": 137.5}]
    }
  }]
}
```

## Visual design

Follows the project's data-viz conventions. The governing idea: this replaces a
TUI, so **density and instant readability beat decoration**.

### Form selection

Chosen by the data's job, not by what looks impressive:

| Data | Form | Not |
|---|---|---|
| GPU util / temp / power — current value + recent trend | **Stat tile** (value + sparkline) | a one-bar bar chart |
| Cluster headline (aggregate tokens/sec) | **Hero figure** (≥48px) | — |
| KV cache used, memory vs. capacity — one ratio against a limit | **Meter** (same-hue track) | a 2-slice pie |
| Process list, "what's running where" | **Table** (`tabular-nums`, sorted by GPU memory) | color-coding N models |
| Utilization / tokens-per-sec over time | **Line chart** (uPlot) | — |

### Color

Uses the reference palette. Two distinct color jobs, never mixed:

**Node identity → categorical slots.** Eight slots, then deliberately none. A
node's slot is its position in `cluster.yml`, so its colour is its line in that
file, and appending a node never repaints an existing one.

The first three are the all-pairs CVD-safe set, and they are what a typical
install sees:

| Slot | Light | Dark |
|---|---|---|
| 1 | `#2a78d6` (blue) | `#3987e5` |
| 2 | `#eb6834` (orange) | `#d95926` |
| 3 | `#1baf7a` (aqua) | `#199e70` |

Validated (`--pairs all`, both modes): CVD ΔE 9.2 light / 9.4 dark, normal-vision
24.0 / 20.9 — all checks pass. One caveat carried forward: aqua sits at 2.74:1 on
the light surface, so **node series must carry visible direct labels** (the relief
rule) — which the design does anyway, since a legend-only cluster chart would be
worse than useless at a glance.

**Why eight and not three.** The original three cycled with
`--series-${slot % 3 + 1}`, which gave a fourth node the first node's colour —
two lines the same shade on one chart, which is worse than an unfamiliar one.
Past eight, a ninth node takes the neutral rather than a repeat: running out of
distinguishable colours is a fact to state, not to hide by reusing one. See
`frontend/src/lib/theme.ts`.

Color follows the *node*, permanently — filtering to two nodes must not repaint
the survivors.

**Health state → status palette** (reserved; never reused as a series color):

| State | Hex | Mapped from ([metrics.md](metrics.md#5-anomaly-thresholds-starting-point-for-phase-3-alerting)) |
|---|---|---|
| good | `#0ca30c` | PSI LOW, clock PASS, temp < 70°C |
| warning | `#fab219` | PSI MOD, temp 70-80°C, mem > 85% |
| serious | `#ec835a` | PSI HIGH, clock LOCKED |
| critical | `#d03b3b` | PSI CRITICAL, clock THROTTLED, temp > 80°C |

Every status indicator ships **icon + label alongside the color** — never color
alone. On the light surface `warning` and `serious` are deliberately sub-3:1, so
the text label is what actually carries the meaning.

**Models are not a categorical dimension.** A router can hold many models and the
set changes as it swaps — assigning hues would blow past the 8-slot ceiling and
repaint on every eviction. Models live in tables, with **emphasis** (one
highlighted, rest gray) when a single model needs to be the subject of a chart.

### Non-negotiables carried into implementation

- **No dual-axis charts, ever.** Tokens/sec and GPU utilization on one plot =
  two charts or index to a common base.
- Legend present for ≥2 series; ≤4 series also direct-labeled.
- Grid/axes recessive (hairline `#e1e0d9` light / `#2c2c2a` dark); text in ink
  tokens, never the series color.
- `font-variant-numeric: tabular-nums` on table columns and axis ticks so
  fast-updating numbers don't jitter horizontally.
- **Dark mode is selected, not auto-flipped** — the dark steps above are their
  own validated set. Support both the OS `prefers-color-scheme` signal and an
  explicit toggle.
- Re-run the palette validator if any color changes.

### Layout

Single-page, no navigation chrome for the primary view:

```
┌──────────────────────────────────────────────────────────────┐
│  HERO: cluster tokens/sec        [3 nodes up]  [live ●]      │
├──────────────────────────────────────────────────────────────┤
│  KPI ROW — one stat-tile group per node                      │
│  ┌ gx10-1 ────┐ ┌ gx10-2 ────┐ ┌ gx10-3 ────┐                │
│  │ util  87%  │ │ util  12%  │ │  ── down ──│  ← status      │
│  │ mem  71%▁▃▅│ │ mem  30%▁▁▂│ │            │    icon+label  │
│  │ 72°C  94W  │ │ 51°C  38W  │ │            │                │
│  └────────────┘ └────────────┘ └────────────┘                │
├──────────────────────────────────────────────────────────────┤
│  WHAT'S RUNNING WHERE (table: node × runtime × model × state)│
├──────────────────────────────────────────────────────────────┤
│  PROCESS LIST (table, sorted by GPU mem — the nvitop view)   │
├──────────────────────────────────────────────────────────────┤
│  TREND (uPlot line, one series per node, direct-labeled)     │
└──────────────────────────────────────────────────────────────┘
```

A down node keeps its tile in place showing its status rather than vanishing —
a missing tile is easy to miss; a red one isn't.

## Open items

- Whether to add a per-node detail drill-down page beyond the single view
  (defer until the single view is in daily use — it may not be needed).
- Chart interaction depth (crosshair/tooltip on the trend chart is the
  baseline; anything more is Phase 4).
