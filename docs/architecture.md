# Architecture

## Recommendation: Prometheus for collection/storage, homegrown app for the UI

Two extremes were on the table:

1. **Pure Grafana + Prometheus** — least custom code, but Grafana doesn't natively
   understand "which model is loaded on which node right now" or "the router just
   swapped models" — those are inference-domain concepts we'd be fighting the
   dashboard-panel model to express well.
2. **Pure from-scratch app** — full control, but reinventing time-series
   scraping/storage/retention across 3 nodes is wasted effort; Prometheus already
   solves that well and is the de facto standard for exactly this (it's also what
   `dcgm-exporter` and vLLM already speak natively).

**Recommendation: hybrid.** Use Prometheus as the collection & storage layer
(scraping every node), and build the "homegrown" part as a small backend + frontend
that queries Prometheus (PromQL) and layers inference-specific views on top —
"what's running where," router swap activity, cluster-wide capacity — that a
generic Grafana panel isn't a good fit for. This also matches the repo's name:
the *dashboard* is homegrown; the *plumbing* underneath it isn't reinvented.

Grafana isn't excluded — since everything lands in Prometheus, nothing stops us
from also pointing Grafana at it later for ad hoc exploration. The homegrown
frontend is for the "at a glance, tailored to this cluster" view.

**Confirmed scope:** the dashboard is meant to fully replace SSH'ing in and
running `nvtop`/`nvitop`/`sparkview` for day-to-day monitoring — not just
complement them. That drives the [live-view fast path](#live-view-fast-path)
below, and it stays strictly read-only (no process/model control actions —
confirmed non-goal, see [requirements.md](requirements.md)).

## Components

```
┌─────────────────────────────────────────────────────────────────┐
│ Node 1 (GX10 #1)         Node 2 (GX10 #2)      Node 3 (GX10 #3) │
│ ┌───────────────────┐    ┌──────────────┐      ┌──────────────┐ │
│ │ llama.cpp router   │    │ llama.cpp    │      │ llama.cpp    │ │
│ │ vLLM container(s)  │    │ vLLM         │      │ vLLM         │ │
│ │ node_exporter      │    │ node_exporter│      │ node_exporter│ │
│ │ dcgm-exporter /    │    │ (same)       │      │ (same)       │ │
│ │  dgx-spark-prom.   │    │              │      │              │ │
│ │ gb10-node-exporter │    │ (same)       │      │ (same)       │ │
│ │  (custom — PSI,    │    │              │      │              │ │
│ │  throttle, spark_  │    │              │      │              │ │
│ │  hwmon power rails)│    │              │      │              │ │
│ │ llama-router-      │    │ (same)       │      │ (same)       │ │
│ │  exporter (custom) │    │              │      │              │ │
│ └─────────┬──────────┘    └──────┬───────┘      └──────┬───────┘ │
└───────────┼──────────────────────┼─────────────────────┼─────────┘
            │            scraped over LAN (~15s, history) │
            │            polled over LAN (~1-2s, live) ───┤
            └──────────────────┬───────────────┬─────────┘
                                ▼               │
                        ┌───────────────┐       │
                        │  Prometheus   │◄──────┘  (history / trends)
                        │ (central)     │
                        └───────┬───────┘
                                │ PromQL
                                ▼
                        ┌────────────────────────┐
                        │ homegrown backend API   │  (FastAPI, proposed)
                        │  - REST: history/trends │
                        │  - WebSocket: live poll  │◄── polls exporters
                        │    fan-out (~1-2s)       │    directly, bypassing
                        └───────┬─────────────────┘    Prometheus for freshness
                                ▼
                        ┌───────────────┐
                        │ homegrown      │  (React/Vite, proposed)
                        │ frontend       │
                        └───────┬───────┘
                                │
                 LAN direct ────┴──── Cloudflare Tunnel + Google OAuth (remote)
```

### Per-node (runs on all 3 GX10s)

- **Existing:** llama.cpp router containers, vLLM containers — unchanged, we're
  only adding scrape targets, not touching the inference stack itself.
- **`node_exporter`** — standard host metrics (CPU/mem/disk/net). ARM64 build
  available, no GB10-specific concerns.
- **`dcgm-exporter` or `dgx-spark-prometheus`** — baseline GPU metrics (standard
  multi-GPU Prometheus plumbing). Evaluate `dgx-spark-prometheus` first since
  it's purpose-built for this hardware; `dcgm-exporter` is the fallback,
  optionally paired with `nvml-unified-shim` to correct its memory reporting.
  See [metrics.md](metrics.md) for the specific caveats.
- **`gb10-node-exporter` (new, ours to build)** — covers what the baseline
  exporter doesn't: UMA-correct memory (`vm.total - vm.available`), PSI memory
  pressure, load-gated clock-throttle state, and — where the `spark_hwmon`
  kernel module ([antheas/spark_hwmon](https://github.com/antheas/spark_hwmon),
  installed via `dkms`) is present — GB10 power-rail detail (`gpu`, `dc_input`,
  `syspl1`, `PROCHOT`, power-limit level, `Tj-rise`). Modeled directly on
  [`sparkview`](https://github.com/parallelArchitect/sparkview)'s
  field-validated technique rather than re-derived from scratch. Also serves
  as the data source for the [live-view fast path](#live-view-fast-path) below,
  polled far more frequently than Prometheus scrapes it.
- **`llama-router-exporter` (new, ours to build)** — a small sidecar that talks to
  the local llama.cpp router's status endpoint to find currently *loaded* models
  only, fans out to `/metrics?model=<name>` for just those, and republishes an
  aggregated Prometheus endpoint. Deliberately does **not** poll models that
  aren't already loaded, to avoid the known autoload/anti-sleep side effect
  (see [metrics.md](metrics.md#3-llamacpp-router-mode-per-node)).
- vLLM needs no sidecar — it already exposes `/metrics` natively; Prometheus
  scrapes it directly.

### Central (one place — see "where does this run" below)

- **Prometheus** — scrapes all of the above across all 3 nodes via a static (or
  file-based service discovery) target list. Handles retention/history.
- **Backend API** — has two distinct jobs, not one:
  - **History/trends (REST):** queries Prometheus over PromQL for anything
    chart-over-time — the normal dashboard-backend pattern.
  - **Live view (WebSocket):** polls `gb10-node-exporter`/vLLM/
    `llama-router-exporter` on each node directly, on a ~1-2s cadence, and
    pushes updates to connected clients — deliberately bypassing Prometheus's
    coarser scrape interval for this path, because "live" is the whole point
    (see [Live-view fast path](#live-view-fast-path)).
  - Proposed: **Python + FastAPI** (good Prometheus client ecosystem,
    async-friendly for fanning out polling requests across 3 nodes, native
    WebSocket support, easy ARM64 support). Open to alternatives — flag if
    you'd rather use Go or Node here.
- **Frontend** — the actual dashboard UI. Proposed: **React + Vite**, or a
  lighter alternative like Svelte/htmx if we want to minimize build tooling for
  a homelab-scale project. Open decision — see [roadmap.md](roadmap.md).

### Live-view fast path

The thing that made `nvtop`/`nvitop`/`sparkview` worth SSH'ing in for is
immediacy: sub-2-second refresh, dense per-process detail, color-coded state
changes you notice instantly. A dashboard that only reflects Prometheus's
15-30s scrape interval would feel laggy by comparison and fail the "full
replacement" goal. So the live view is architecturally separate from the
history view:

- Backend polls each node's exporters directly (not through Prometheus) on a
  ~1-2s interval and pushes deltas over a WebSocket to connected clients.
  Only runs this tight loop for nodes/panels actually being viewed — no point
  polling at 1s when no one has the dashboard open.
- Required panel: **per-node process list**, sorted by GPU memory usage
  (mirrors `nvitop`/`sparkview`'s process view) — process name, PID, GPU
  memory, which runtime/model it belongs to. Display-only, no kill/control
  actions (confirmed non-goal).
- Required panel: GPU utilization/memory/temp/power tiles per node, with
  color-coded thresholds matching the [anomaly thresholds](metrics.md#5-anomaly-thresholds-starting-point-for-phase-3-alerting)
  already validated by sparkview (e.g. temp > 80°C, PSI ≥ MOD, THROTTLED clock
  state) — the same at-a-glance red/yellow/green signal a TUI gives you, not
  just a number.
- Prometheus/PromQL remains the source for anything historical (trend lines,
  "what did GPU util look like an hour ago") — the live path never needs to
  answer "what happened before now."

### Where does the central stack run?

Open decision (tracked in [roadmap.md](roadmap.md#open-decisions)): running
Prometheus + backend + frontend *on* one of the GX10 nodes is simplest to start
(footprint is small relative to a GB10's capacity) but means monitoring goes
down if that node reboots/is under heavy inference load, and it's monitoring
infra competing — even lightly — with inferencing workload on hardware whose
job is inferencing. If there's a spare always-on machine on the LAN (NAS, mini
PC, Raspberry Pi), that's the cleaner home for the central stack. Default to
running it on Node 1 for the MVP and revisit once the 3-node cluster is up.

## Scaling from 1 → 3 nodes

The design goal is that adding node 2 and node 3 is a **config change, not a code
change**:

- Prometheus target list gains 2 entries (or, better, file-based service
  discovery reading a simple `nodes.yaml` inventory file — see
  [roadmap.md](roadmap.md)).
- Every panel/query in the backend is written in terms of a `node` label from the
  start (even with 1 node today), so nothing needs to change shape later —
  it just starts returning more rows.
- Per-node exporters are identical containers/config across all 3 GX10s (same
  Compose service definitions), so bringing up a new node is "copy the compose
  file, adjust the node name."

## Auth / access

- **LAN:** no auth requirement — reachable directly, matches current usage.
- **Remote (Cloudflare Tunnel + Google OAuth):** Cloudflare Access sits in front
  of the tunnel and handles the Google OAuth challenge before traffic ever
  reaches the dashboard — the backend doesn't need to implement login. For
  defense-in-depth, the backend *can* optionally validate the
  `Cf-Access-Jwt-Assertion` header against Cloudflare's public keys to confirm a
  request genuinely came through Access and not some other path to the same
  port — worth doing before the dashboard carries anything sensitive, not
  required for the MVP.
- The dashboard should be written assuming it **could** be reached without the
  tunnel (e.g. someone on the LAN, or a misconfiguration) — so no secrets should
  ever be gated on "well, OAuth already checked this upstream." Confirmed
  strictly read-only (no process/model control actions), which keeps the blast
  radius low regardless of the auth path.

## Storage / retention

Prometheus's local TSDB is the source of truth for metric history; no separate
database needed for time-series data. A default retention window (e.g. 15-30
days) is a config knob, not an architectural decision — revisit once we know
actual disk usage on real data. If the backend needs to persist anything
*non*-time-series (e.g. a manually-curated node inventory, or a log of router
swap events beyond what Prometheus counters capture), a local SQLite file is
enough at this scale — no need for a database server.
