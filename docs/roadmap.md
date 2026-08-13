# Roadmap

Phased so that we get a useful dashboard on the single existing node quickly,
before the other 2 GX10 units even arrive.

## Phase 0 — Project setup (this session)

- [x] Requirements, architecture, and metrics docs (this repo).
- [ ] Mirror phases below as Forgejo issues/milestones for tracking.

## Phase 1 — Single-node MVP

Goal: something real running on the existing GX10, informative for day-to-day use.

- [ ] Stand up `node_exporter` on the GX10.
- [ ] Evaluate `dgx-spark-prometheus` vs. `dcgm-exporter` for GPU metrics; validate
  memory numbers against `nvidia-smi --query-compute-apps` (unified-memory
  caveat — see [metrics.md](metrics.md)).
- [ ] Confirm vLLM containers already expose `/metrics` and add them as
  Prometheus scrape targets.
- [ ] Build `llama-router-exporter` sidecar (custom) for llama.cpp router-mode
  aggregation, being careful not to trigger autoload/anti-sleep side effects.
- [ ] Stand up Prometheus, scraping all of the above on the one node.
- [ ] Backend API (FastAPI, proposed) with a first cut of endpoints: node health,
  GPU utilization/memory, loaded models + their live stats.
- [ ] Frontend MVP: single-node view — GPU tiles, loaded-models table,
  tokens/sec, request queue depth.

## Phase 2 — Multi-node cluster

Triggered by the 2 additional GX10 units arriving.

- [ ] Roll out the same per-node exporter stack to nodes 2 and 3 (should be a
  copy-paste of the Phase 1 Compose config).
- [ ] Move Prometheus target list to file-based service discovery
  (`nodes.yaml`-style inventory) instead of hardcoded targets.
- [ ] Extend backend/frontend to aggregate across nodes: cluster-wide capacity,
  "what's running where" table, per-node health/liveness panel.
- [ ] Decide + implement where the central stack (Prometheus/backend/frontend)
  actually lives — see [open decisions](#open-decisions) below.

## Phase 3 — Remote access & hardening

- [ ] Confirm dashboard works correctly through the existing Cloudflare Tunnel +
  Google OAuth path.
- [ ] (Optional, defense-in-depth) Validate `Cf-Access-Jwt-Assertion` in the
  backend.
- [ ] Set real Prometheus retention based on observed disk usage.
- [ ] Basic alerting for the things that actually matter at 2am: node down, GPU
  temp/power outlier, disk filling up on a node.

## Phase 4 — Polish / nice-to-haves (unscheduled)

- [ ] Historical trend views (not just live state) — token throughput over time,
  GPU utilization trends.
- [ ] Router swap event log/timeline (not just a gauge of "loaded now").
- [ ] Optional Grafana pointed at the same Prometheus for ad hoc exploration.
- [ ] Job-level drill-down (per-request tracing), if useful.

## Open decisions

These are flagged inline in the other docs too — collected here so they don't
get lost:

1. **Backend/frontend language & framework.** Proposed FastAPI + React/Vite in
   [architecture.md](architecture.md); open to Go/Node or a lighter frontend
   (Svelte/htmx) if preferred.
2. **Where the central Prometheus/backend/frontend stack runs** — on one of the
   3 GX10 nodes (simplest) vs. a separate always-on host (cleaner isolation).
   See [architecture.md](architecture.md#where-does-the-central-stack-run).
3. **`dcgm-exporter` vs. `dgx-spark-prometheus`** for GPU metrics on GB10 — needs
   hands-on evaluation against real hardware, not just docs research.
4. **Cross-node orchestration** (still Compose-per-host vs. eventually Swarm/k8s)
   is out of scope for *this* project but affects how much Prometheus service
   discovery needs to do — revisit if orchestration changes.
