# Architecture

## Recommendation: Prometheus for collection/storage, homegrown app for the UI

Two extremes were on the table:

1. **Pure Grafana + Prometheus** — least custom code, but Grafana doesn't natively
   understand "which model is loaded on which node right now" or "the router just
   swapped models" — those are inference-domain concepts we'd be fighting the
   dashboard-panel model to express well.
2. **Pure from-scratch app** — full control, but reinventing time-series
   scraping/storage/retention across a handful of nodes is wasted effort; Prometheus already
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

Every box below is a Docker container — see [deployment.md](deployment.md) for
the Compose layout, the containerization approach for each piece, and why
GB10 power-rail telemetry (`spark_hwmon`) was deliberately left out since it
requires a host kernel module.

```
┌──────────────────────────────────────────────────────────────────┐
│  GX10 nodes — byte-identical Compose stack (only NODE_ID differs) │
│                                                                  │
│  Node 1 (GX10 #1)       Node 2 (GX10 #2)     Node 3 (GX10 #3)    │
│  ┌──────────────────┐   ┌───────────────┐    ┌───────────────┐   │
│  │ llama.cpp router │   │ (same)        │    │ (same)        │   │
│  │ vLLM container(s)│   │               │    │               │   │
│  │ ─────────────────│   │               │    │               │   │
│  │ node_exporter    │   │               │    │               │   │
│  │ spark-dash-agent │   │               │    │               │   │
│  │  collectors:     │   │               │    │               │   │
│  │   gpu (NVML)     │   │               │    │               │   │
│  │   memory (UMA)   │   │               │    │               │   │
│  │   psi            │   │               │    │               │   │
│  │   clock          │   │               │    │               │   │
│  │   llama_router   │   │               │    │               │   │
│  └────────┬─────────┘   └───────┬───────┘    └───────┬───────┘   │
└───────────┼─────────────────────┼────────────────────┼───────────┘
            │   scrape ~15s (history) + poll ~1-2s (live)          │
            └─────────────────────┼────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│  Central stack — dedicated Proxmox VM (NOT on any GX10)          │
│                                                                  │
│    ┌───────────────┐                                             │
│    │  Prometheus   │  scrapes all nodes; TSDB = history/trends    │
│    └───────┬───────┘                                             │
│            │ PromQL                                              │
│            ▼                                                     │
│    ┌──────────────────────────┐                                  │
│    │ spark-dash-backend       │  (FastAPI)                       │
│    │  - REST: history/trends  │                                  │
│    │  - WebSocket: live       │──── polls agents directly,       │
│    │    (shared poller ~1-2s) │     bypassing Prometheus         │
│    │  - serves Svelte assets  │     for freshness                │
│    └───────────┬──────────────┘                                  │
│                │                                                 │
│    ┌───────────┴──────────┐                                      │
│    │ cloudflared (exists) │                                      │
│    └───────────┬──────────┘                                      │
└────────────────┼─────────────────────────────────────────────────┘
                 │
  LAN direct ────┴──── Cloudflare Tunnel + Google OAuth (remote)
```

### Per-node (runs on all 3 GX10s)

- **Existing:** llama.cpp router containers, vLLM containers — unchanged, we're
  only adding scrape targets, not touching the inference stack itself.
- **`node_exporter`** — standard host metrics (CPU/mem/disk/net). ARM64 build
  available, no GB10-specific concerns.
- **`spark-dash-agent` (new, ours to build)** — one container per node, with
  internal collector modules. Fully containerized: only needs `/proc`, `/sys`,
  and NVML/`nvidia-smi` access via the Container Toolkit, no host install.
  Modeled directly on
  [`sparkview`](https://github.com/parallelArchitect/sparkview)'s
  field-validated technique rather than re-derived from scratch.
  - `gpu` — NVML via `nvitop`: utilization, temperature, power, clocks,
    per-process GPU memory
  - `memory` — UMA-correct calc (`vm.total - vm.available`); raw NVML memory is
    meaningless on GB10
  - `psi` — `/proc/pressure/memory`
  - `clock` — load-gated throttle state machine
  - `llama_router` — talks to the local llama.cpp router's status endpoint to
    find currently *loaded* models only, then fans out to
    `/metrics?model=<name>` for just those. Deliberately does **not** poll
    models that aren't already loaded, to avoid the known autoload/anti-sleep
    side effect (see [metrics.md](metrics.md#3-llamacpp-router-mode-per-node)).
    Reports nothing on a node not running llama.cpp, so the image is identical
    everywhere.

  Deliberately excludes GB10 power-rail/`PROCHOT` telemetry (`spark_hwmon`) —
  that requires a real kernel module and was descoped to keep the base OS
  untouched; see [deployment.md](deployment.md). Also serves as the data source
  for the [live-view fast path](#live-view-fast-path) below, polled far more
  frequently than Prometheus scrapes it.
- **Third-party GPU exporters (`dcgm-exporter` / `dgx-spark-prometheus`) will
  not ship** — decided 2026-08-21 after three phases of deferral. The agent
  reads NVML directly, and `dcgm-exporter`'s headline advantage (GPU memory) is
  precisely what unified memory breaks on GB10. Its remaining draw is memory
  bandwidth, which is a genuine blind spot and still not worth a resident
  daemon on the inference node. See
  [deployment.md](deployment.md#gpu-baseline-exporter--will-not-ship-decided-2026-08-21).
- vLLM needs no sidecar — it already exposes `/metrics` natively; Prometheus
  scrapes it directly.

### Central (dedicated Proxmox VM — see [deployment.md](deployment.md#central-stack--a-dedicated-proxmox-vm-settled))

- **Prometheus** — scrapes all of the above across every node via a static (or
  file-based service discovery) target list. Handles retention/history.
- **Backend API** — has two distinct jobs, not one:
  - **History/trends (REST):** queries Prometheus over PromQL for anything
    chart-over-time — the normal dashboard-backend pattern.
  - **Live view (WebSocket):** polls each node's `spark-dash-agent` (and vLLM)
    directly, on a ~1-2s cadence, and
    pushes updates to connected clients — deliberately bypassing Prometheus's
    coarser scrape interval for this path, because "live" is the whole point
    (see [Live-view fast path](#live-view-fast-path)).
  - **Python 3.12 + FastAPI** (settled). Shares a `common/` package with the
    agent so metric models don't drift. See
    [app-design.md](app-design.md) for the full API surface.
- **Frontend** — the actual dashboard UI. **Svelte 5 + Vite + TypeScript**,
  charts via uPlot (settled). Built to static assets and served by the backend
  container — one less service, no CORS, same-origin WebSocket. See
  [app-design.md](app-design.md).

### Live-view fast path

The thing that made `nvtop`/`nvitop`/`sparkview` worth SSH'ing in for is
immediacy: sub-2-second refresh, dense per-process detail, color-coded state
changes you notice instantly. A dashboard that only reflects Prometheus's
15-30s scrape interval would feel laggy by comparison and fail the "full
replacement" goal. So the live view is architecturally separate from the
history view:

- Backend polls each node's agent directly (not through Prometheus) on a
  ~1-2s interval and pushes a full snapshot over a WebSocket to connected
  clients (full snapshot, not deltas — a few KB at 1Hz makes both sides
  stateless; see [app-design.md](app-design.md#websocket--live-view)). One
  shared poller feeds all subscribers, and the loop idles entirely when no
  clients are connected — no point polling at 1s when no one has the
  dashboard open.
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

### Where does the central stack run? (settled)

**A dedicated VM on the existing Proxmox cluster — never on a GX10.** The
deciding argument is failure domains, not resources: "node down" is a primary
alert, so hosting Prometheus on node 1 means a node-1 crash destroys both the
node and the history explaining why. It also keeps every GX10
interchangeable and keeps `cloudflared` (and therefore any externally-reachable
surface) off the inference hardware entirely. Full reasoning, sizing, and the
dead-man's-switch caveat are in
[deployment.md](deployment.md#central-stack--a-dedicated-proxmox-vm-settled).

## Scaling by node count

The design goal was that adding a node is a **config change, not a code
change**. It held: nodes 2 and 3 arrived as entries in `cluster.yml` and
nothing else. What follows is why, and it applies equally to a fourth:

- The Prometheus target list gains an entry per node. This is now file-based
  service discovery generated from `cluster.yml`, so the list is written rather
  than maintained by hand.
- Every panel/query in the backend is written in terms of a `node` label, and
  was from the first single-node build, so nothing changed shape when the
  cluster arrived — the same queries simply return more rows.
- Per-node containers/config are byte-identical on every GX10 — `NODE_ID` is
  optional and defaults to the hostname — so bringing up a new node is
  genuinely "copy the directory, set `BACKEND_URL`".

## Auth / access

- **LAN:** no auth requirement — reachable directly, matches current usage.
  Prometheus (`:9090`) and Alertmanager (`:9093`) are LAN-only and rely on that
  boundary; neither has authentication of its own. Only the dashboard
  (`:8080`) is published externally.
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
