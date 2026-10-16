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

A field per engine under `runtimes`, even though vLLM, SGLang and TensorFold
carry the same shape. The alternative — one `engines` list with a `runtime` discriminator —
would rename the `sparkdash_vllm_*` metric family that the alert rules and every
recorded series are written against. The collector is shared (one spec of metric
names per engine); the wire is per-engine. `kv_cache_pct` is null for an engine
that reports no occupancy — see [metrics](metrics.md#sglang-per-instance--same-shape-different-names).
| `GET /api/history?metric=&node=&from=&to=&step=` | Time-series for trend charts; thin PromQL wrapper |
| `GET /api/latest?metric=` | Prometheus's latest reading of the five metrics the live snapshot lacks (CPU clock, swap I/O, CPU and I/O pressure, disk busy), with when each node was scraped, for System Activity's gauges (roadmap BA2) |
| `POST /api/assistant/ask` | A question answered from the window's alerts, events and series by the configured model (roadmap AY); `GET /api/assistant/status`, `PUT`/`DELETE /api/assistant/config`, `POST /api/assistant/models`, `POST /api/assistant/test` set it up |
| `POST /mcp` | Tools for agents over MCP, streamable HTTP, behind a bearer key (roadmap BB): fourteen read-only tools, plus `promql` (read-only) and `add_note` (the one write) behind their own switches, three report prompts and three reference guides; `GET /api/mcp/status`, `PUT /api/mcp/config` (`enabled`, `promql`, `notes`), `POST /api/mcp/key` set it up |
| `GET /api/reports` | Kept reports, newest first (roadmap BI); `GET /api/reports/{id}` one, `DELETE` removes it, `GET /api/reports/{id}/download?format=pdf\|md\|html\|csv\|json` (`pdf` takes `orientation=landscape\|portrait` and `inline=1` for the preview; `html` takes `print=1`) |
| `POST /api/reports/build` | A report now: `start`/`end` or `minutes`, `nodes`, `sections`, `title`, `narrative`, `keep`; `POST /api/reports/keep` keeps one built earlier |
| `GET /api/reports/meta`, `PUT /api/reports/config` | Sections, nodes, the zone in use and the host's, schedules with their next run; the whole `reports:` block. `POST /api/reports/schedules/{id}/run` runs one now |
| `GET /api/setup` | The onboarding checklist (roadmap BE): each step's level, state (`done`, `attention`, `todo`, `info`), why it matters, and the Settings pane or guide for it, worked out from the system |
| `GET /api/notifications/status` | Where alerts go: Alertmanager's default receiver and channels, the ntfy server and topic masked, whether the backend can write the topic file (roadmap BF). Never the topic |
| `GET`/`PUT /api/notifications/topic` | The topic, asked for on purpose to subscribe; `PUT {server, topic?}` writes a new one, random unless given. `POST /api/notifications/test` sends an Alertmanager-shaped test |
| `DELETE /api/notes` | Removes every note agents added to the timeline (roadmap BB17); notes arrive with `/api/annotations` as kind `note` |
| `GET /api/latency` | Time to first token per model, p50 and p95 over 15 minutes, with speculative-decoding acceptance; the header's busiest model |
| `GET /health` | Liveness + self-assessment, for the external UptimeKuma check |

`/health` is deliberately more than a bare `200 OK` — it reports **degraded**
when Prometheus is unreachable or the live-poller loop has stalled, so a
backend that's running but wedged is caught rather than passing a naive check.
This is what [UptimeKuma watches](deployment.md#monitoring-the-monitor--existing-uptimekuma-instance)
to close the "who monitors the monitor" gap.

### REST — client stats (optional)

From a LiteLLM gateway, when one is set up
([deployment](deployment.md#client-stats--optional),
[metrics](metrics.md#litellm-gateway--optional-per-client)). The address and
the switch live in `cluster.yml`'s `gateway:` block, which the node editor
leaves alone when it saves.

| Endpoint | Does |
|---|---|
| `GET /api/clients?minutes=` | The Clients card's rows, 5 minutes to 30 days. Empty while off; 503 when Prometheus is down. |
| `GET /api/clients/status` | `capability` (an address is set), `enabled` (the switch), `url`, `error`, `cluster_file`, and `state`, which is `/health`'s value. Asks Prometheus, never the gateway, so it can be polled freely. |
| `PUT /api/clients/config` | `{url}` sets the address (a trailing `/v1` is dropped); `{url: null}` removes it. `enabled`, if omitted, keeps the switch as it was. 409 on a deployment with no `cluster.yml`. |
| `POST /api/clients/enabled` | `{enabled}`. Answers while off, so off is never a state you cannot leave. 409 with no address yet. |
| `POST /api/clients/test` | `{url}`, unsaved. Requests `/health/liveliness` and `/metrics` on the gateway and nothing else: LiteLLM's `/health` sends a real request to every model. |

Every write returns the new status and re-renders Prometheus's target list, so
the scrape starts or stops within one refresh (30 s).

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
                  "prompt_tokens_per_sec": 0.0, "tokens_per_sec": 137.5}],
      "tensorfold": []
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
| 1 | `#f623b0` | `#7261c1` |
| 2 | `#841a84` | `#2ca79e` |
| 3 | `#2395ff` | `#2c95ff` |

**Status colours own their hues, and node identity stays out of them.** Green,
amber and red mean *how it is*; node colour means *which machine*. Those were
confusable — measured across the seven themes, every one had node slots inside
ΔE 12 of a status colour, worst case ΔE 0.4, which is the same colour. Node
slots now live on the cyan→blue→purple→magenta arc, and `scripts/palette_check.py`
enforces the separation so it cannot drift back.

Validated by that script on every theme: lightness band, chroma floor, ≥3:1
against the theme's own surface, adjacent-pair separation under protan and
deutan simulation, and ≥12 ΔE from every status colour. Node series still carry
visible direct labels (the relief rule) — a legend-only cluster chart would be
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

| State | Hex | Mapped from ([metrics.md](metrics.md#5-anomaly-thresholds-and-what-measurement-did-to-them)) |
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

Single-page, no navigation chrome for the primary view. Below the header, the
node frames, then the cards, each full width or in one of two columns:

```
┌──────────────────────────────────────────────────────────────────────┐
│ spark-dash  decode tok/s · first token · free block · nodes · prefill│
├──────────────────────────────────────────────────────────────────────┤
│ ┌ DANFLASHES · 2 nodes pooled ───────┐ ┌ STANDALONE ┐                │
│ │ pooled memory band                 │ │            │   beside the   │
│ │ [sparketa]        [sparkjr]        │ │ [sparky]   │   nodes (wide  │
│ └────────────────────────────────────┘ └────────────┘   monitors)    │
├──────────────────────────────────────────────────────────────────────┤
│ SYSTEM ACTIVITY  a chart per metric    │ NETWORK ACTIVITY  a chart   │
│                                        │ per interface group         │
├────────────────────────────────────────┼─────────────────────────────┤
│ GPU PROCESSES                          │ MODELS                      │
├────────────────────────────────────────┴─────────────────────────────┤
│ TEMPERATURES  a table per sensor group                               │
└──────────────────────────────────────────────────────────────────────┘
```

A down node keeps its tile in place showing its status rather than vanishing —
a missing tile is easy to miss; a red one isn't.

**Compact node cards share one grid.** At 1, 2, 4 or 8 columns by width, each
cluster spans one column per member and each standalone node gets a frame of
its own. Members are spaced so every card lands on the outer columns, so a
member is exactly as wide as a standalone node (measured 377.3px each at 1680px
wide, against 397 and 420 before). Frames on one row share three rows,
header, memory band and cards, so they end level and every card starts at the
same height. When the frames need half the columns or fewer, the top of the
page splits: nodes on the left at half the column count, and on the right a
column, *beside the nodes*, that takes half-width cards like the columns below.

**Charts.** One time axis per grid, on its bottom row; the rows above give up
that band, so every plot is the same height (150px at the default 180px,
where it used to be 65 of 132). Columns come from the card's own width, at
least 300px a chart. System Activity fits each axis to its data, never
narrower than the metric's minimum span (25 points for GPU utilization, 20 for
other percentages, 15°C, 50W, 500MHz), with **Full** for the fixed axes.
Network Activity draws a chart per interface group, a line per node: received
above zero, sent below, each side reaching its own peak but keeping a fifth of
the height. Its **log** axis is asinh, linear inside 1 kb/s, with gridlines on
decades. A node's interfaces in a group are added together, and a ConnectX
port is two interfaces, so multicast reaching it counts on both (about
40 kb/s here, against 200 Gb/s links).

**Optional cards are the exception, on purpose.** A card whose data may not
exist on this install is off the page and out of the `+ element` menu while
its feature is off, and keeps its place in the saved layout, so switching the
feature back on returns it to where it was. The Clients card is the first:

- **Harness** — named from the User-Agent. An `sdk` tag means it is a
  library's default, which says which SDK and not which program; the hover
  shows the raw string. *Anthropic-API client* is a failed `/v1/messages`
  request, which LiteLLM passes on without its User-Agent. When the gateway
  recognises the program from its requests (see
  [client stats](deployment.md#client-stats--optional)), that name wins:
  Hermes Agent rather than "OpenAI SDK (Python)", with its side calls on a
  row of their own, tagged *side calls*.
- **Client** — the machine: a node's id if it is one, else its reverse-DNS
  name, else its address. Hover for the full name and the IP. For an agent
  sending an SDK default, this is the column that says who it was, which is
  why both are required columns.
- **Model** — what was asked for, prefix included. *No such model* is a name
  no gateway route matched, refused before reaching an engine.
- **Engine** — *node · runtime*, joined from the gateway's `api_base` to
  `cluster.yml`, or the bare address for an endpoint this dashboard does not
  monitor.
- **Listed** — off by default, and on by itself while a row has a model its
  engine no longer lists: a request under a retired name that still succeeds,
  because some engines answer any name. Hover for what the engine serves.
- **Tokens in**, **tokens out** — prompt and completion tokens in the window,
  when the gateway counts them per client; "—" otherwise.
- **Requests**, **req/min**, **failed**, **active** — over the window picked
  in the card's header (1h to 7d). Requests include failed attempts; failed
  breaks down by status on hover; active means something was sent in the last
  five minutes. The header says *counting since* when the gateway's scrape
  began inside the window.

## Open items

- Whether to add a per-node detail drill-down page beyond the single view
  (defer until the single view is in daily use — it may not be needed).
- Chart interaction depth (crosshair/tooltip on the trend chart is the
  baseline; anything more is Phase 4).
