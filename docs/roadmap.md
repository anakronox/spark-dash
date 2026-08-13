# Roadmap

Phased so that we get a useful dashboard on the single existing node quickly,
before the other 2 GX10 units even arrive.

## Phase 0 — Project setup (this session)

- [x] Requirements, architecture, and metrics docs (this repo).
- [ ] Mirror phases below as Forgejo issues/milestones for tracking.

## Phase 1 — Single-node MVP

Goal: something real running on the existing GX10, informative for day-to-day use.

- [ ] Write the per-node Compose file (`node-exporter`, GPU baseline exporter,
  `gb10-node-exporter`, `llama-router-exporter`, joined to the existing
  inference stack's Docker network) — see [deployment.md](deployment.md).
  Everything Docker-only; no host installs beyond Docker + NVIDIA Container
  Toolkit (already present).
- [ ] Stand up `node_exporter` container on the GX10.
- [ ] Evaluate `dgx-spark-prometheus` vs. `dcgm-exporter` (+ `nvml-unified-shim`)
  for baseline GPU metrics; validate memory numbers against
  `nvidia-smi --query-compute-apps` (unified-memory caveat — see
  [metrics.md](metrics.md)). If `dgx-spark-prometheus` wins, package it in our
  own Dockerfile rather than its upstream systemd install path (see
  [deployment.md](deployment.md)).
- [ ] Build `gb10-node-exporter` (custom, containerized): UMA-correct memory,
  PSI pressure, clock-throttle state — modeled on `sparkview`'s validated
  technique. GB10 power-rail/`PROCHOT` telemetry via `spark_hwmon` was
  evaluated and deliberately descoped (real kernel module, no container
  workaround, conflicts with keeping the base OS untouched) — see
  [deployment.md](deployment.md#spark_hwmon--evaluated-deliberately-descoped).
- [ ] Confirm vLLM containers already expose `/metrics` and add them as
  Prometheus scrape targets.
- [ ] Build `llama-router-exporter` sidecar (custom) for llama.cpp router-mode
  aggregation, being careful not to trigger autoload/anti-sleep side effects.
- [ ] Stand up Prometheus, scraping all of the above on the one node.
- [ ] Backend API (FastAPI, proposed): REST endpoints backed by Prometheus for
  history, plus a WebSocket live-poll path (~1-2s) hitting `gb10-node-exporter`/
  vLLM/`llama-router-exporter` directly for the near-real-time view. See
  [architecture.md](architecture.md#live-view-fast-path).
- [ ] Frontend MVP: single-node live view — GPU tiles (color-coded against the
  [anomaly thresholds](metrics.md#5-anomaly-thresholds-starting-point-for-phase-3-alerting)),
  per-process list sorted by GPU memory, loaded-models table, tokens/sec,
  request queue depth. This is the "replaces SSH + nvtop/nvitop/sparkview"
  milestone — worth actually using day-to-day before calling Phase 1 done.

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
  temp/power outlier, disk filling up on a node. Start from the
  [anomaly thresholds](metrics.md#5-anomaly-thresholds-starting-point-for-phase-3-alerting)
  `sparkview` already field-validated (PSI ≥ MOD, THROTTLED/LOCKED clock under
  load, mem >85% + swap active, temp >80°C) rather than guessing from scratch —
  note `PROCHOT` isn't available to us since `spark_hwmon` is out of scope.

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
3. **`dcgm-exporter` vs. `dgx-spark-prometheus`** for *baseline* GPU metrics on
   GB10 — needs hands-on evaluation against real hardware, not just docs
   research. (Narrower than before: the GB10-specific signals — UMA memory,
   PSI, clock throttle — are now planned as our own `gb10-node-exporter`
   regardless of which baseline exporter wins, modeled on `sparkview`'s
   validated approach. See [metrics.md](metrics.md) and
   [architecture.md](architecture.md).)
4. **Cross-node orchestration** (still Compose-per-host vs. eventually Swarm/k8s)
   is out of scope for *this* project but affects how much Prometheus service
   discovery needs to do — revisit if orchestration changes.
