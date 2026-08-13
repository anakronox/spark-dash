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
│ │ llama-router-      │    │ (same)       │      │ (same)       │ │
│ │  exporter (custom) │    │              │      │              │ │
│ └─────────┬──────────┘    └──────┬───────┘      └──────┬───────┘ │
└───────────┼──────────────────────┼─────────────────────┼─────────┘
            │            scraped over LAN                │
            └──────────────────┬───────────────┬─────────┘
                                ▼               │
                        ┌───────────────┐       │
                        │  Prometheus   │◄──────┘
                        │ (central)     │
                        └───────┬───────┘
                                │ PromQL
                                ▼
                        ┌───────────────┐
                        │ homegrown      │  (FastAPI, proposed)
                        │ backend API    │
                        └───────┬───────┘
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
- **`dcgm-exporter` or `dgx-spark-prometheus`** — GPU metrics. Evaluate
  `dgx-spark-prometheus` first since it's purpose-built for this hardware and
  should handle the unified-memory quirks better; fall back to `dcgm-exporter` +
  `nvidia-smi --query-compute-apps` cross-checks if it's insufficient. See
  [metrics.md](metrics.md) for the specific caveats.
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
- **Backend API** — queries Prometheus over PromQL, adds the "what's running
  where" aggregation, exposes a clean JSON API to the frontend. Proposed:
  **Python + FastAPI** (good Prometheus client ecosystem, async-friendly for
  fanning out queries, easy ARM64 support). Open to alternatives — flag if you'd
  rather use Go or Node here.
- **Frontend** — the actual dashboard UI. Proposed: **React + Vite**, or a
  lighter alternative like Svelte/htmx if we want to minimize build tooling for
  a homelab-scale project. Open decision — see [roadmap.md](roadmap.md).

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
  tunnel (e.g. someone on the LAN, or a misconfiguration) — so no secrets or
  destructive actions should ever be gated on "well, OAuth already checked this
  upstream." Read-only dashboard today, so the blast radius is low, but worth
  stating as a principle for when/if control actions get added later.

## Storage / retention

Prometheus's local TSDB is the source of truth for metric history; no separate
database needed for time-series data. A default retention window (e.g. 15-30
days) is a config knob, not an architectural decision — revisit once we know
actual disk usage on real data. If the backend needs to persist anything
*non*-time-series (e.g. a manually-curated node inventory, or a log of router
swap events beyond what Prometheus counters capture), a local SQLite file is
enough at this scale — no need for a database server.
