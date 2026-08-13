# Roadmap

Phased so that we get a useful dashboard on the single existing node quickly,
before the other 2 GX10 units even arrive.

## Phase 0 — Project setup (this session)

- [x] Requirements, architecture, metrics, deployment, and app-design docs.
- [ ] Mirror phases below as Forgejo issues/milestones for tracking.

## Phase 1 — Single-node MVP

Goal: something real running on the existing GX10, informative for day-to-day use.

- [ ] Scaffold the monorepo layout (`common/`, `agent/`, `backend/`,
  `frontend/`, `deploy/`) — see [app-design.md](app-design.md#repo-layout).
- [ ] Provision the monitoring VM on Proxmox (Brian deploys; ~2 vCPU / 4GB /
  50GB, Docker host) — see
  [deployment.md](deployment.md#central-stack--a-dedicated-proxmox-vm-settled).
- [ ] Write the per-node Compose file (`node-exporter` + `spark-dash-agent`,
  joined to the existing inference stack's Docker network) — see
  [deployment.md](deployment.md). Everything Docker-only; no host installs
  beyond Docker + NVIDIA Container Toolkit (already present).
- [ ] Build `spark-dash-agent` (custom, containerized) with its collector
  modules: `gpu` (NVML via `nvitop`, incl. per-process attribution), `memory`
  (UMA-correct calc), `psi`, `clock` (throttle state machine), `llama_router`
  (loaded-models-only polling — must not trigger the autoload/anti-sleep bug).
  Modeled on `sparkview`'s validated technique. GB10 power-rail/`PROCHOT`
  telemetry via `spark_hwmon` was evaluated and deliberately descoped (real
  kernel module, no container workaround, conflicts with keeping the base OS
  untouched) — see
  [deployment.md](deployment.md#spark_hwmon--evaluated-deliberately-descoped).
- [ ] Validate the agent's memory numbers against
  `nvidia-smi --query-compute-apps` on real hardware (unified-memory caveat —
  see [metrics.md](metrics.md)).
- [ ] Confirm vLLM containers already expose `/metrics` and add them as
  Prometheus scrape targets.
- [ ] Stand up Prometheus on the monitoring VM, scraping the GX10 over the LAN.
- [ ] Backend API (Python 3.12 + FastAPI): REST endpoints backed by Prometheus
  for history, plus a WebSocket live path (~1-2s, full snapshot, one shared
  poller) hitting each node's agent (and vLLM) directly.
  See [app-design.md](app-design.md#api-surface).
- [ ] Frontend MVP (Svelte 5 + Vite + TS, uPlot for charts): single-node live
  view — GPU stat tiles color-coded against the
  [anomaly thresholds](metrics.md#5-anomaly-thresholds-starting-point-for-phase-3-alerting),
  per-process list sorted by GPU memory, loaded-models table, tokens/sec,
  request queue depth. Follow the form/color rules in
  [app-design.md](app-design.md#visual-design). This is the "replaces SSH +
  nvtop/nvitop/sparkview" milestone — worth actually using day-to-day before
  calling Phase 1 done.

## Phase 2 — Multi-node cluster

Triggered by the 2 additional GX10 units arriving.

- [ ] Roll out the same per-node stack to nodes 2 and 3 — a literal copy of the
  Phase 1 Compose file with `NODE_ID` changed, nothing else.
- [ ] Move Prometheus target list to file-based service discovery
  (`nodes.yaml`-style inventory) instead of hardcoded targets.
- [ ] Extend backend/frontend to aggregate across nodes: cluster-wide capacity,
  "what's running where" table, per-node health/liveness panel.
- [ ] Point the central Prometheus at all 3 nodes (the VM already exists from
  Phase 1 — no migration needed, which is part of why it isn't on a GX10).

## Phase 3 — Remote access & hardening

- [ ] Confirm dashboard works correctly through the existing Cloudflare Tunnel +
  Google OAuth path.
- [ ] (Optional, defense-in-depth) Validate `Cf-Access-Jwt-Assertion` in the
  backend.
- [ ] Set real Prometheus retention based on observed disk usage.
- [ ] External dead-man's-switch so a monitoring-VM outage is itself detectable
  (the one gap the stack can't close from inside — see
  [deployment.md](deployment.md#monitoring-the-monitor)).
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
- [ ] Evaluate `dcgm-exporter` / `dgx-spark-prometheus` for deep GPU profiling
  (SM/tensor-core activity, memory bandwidth) — deferred from Phase 1 since the
  agent covers the basics and DCGM's memory reporting is broken by GB10 unified
  memory anyway. If `dgx-spark-prometheus` is adopted, package it in our own
  Dockerfile rather than its upstream systemd install path.

## Open decisions

These are flagged inline in the other docs too — collected here so they don't
get lost:

1. ~~**Backend/frontend language & framework.**~~ **Settled:** Python 3.12 +
   FastAPI (backend and agent, sharing a `common/` package) and Svelte 5 +
   Vite + TypeScript with uPlot for charts. See [app-design.md](app-design.md)
   for the reasoning, API surface, and visual design rules.
2. ~~**Where the central Prometheus/backend stack runs.**~~ **Settled:** a
   dedicated VM on the existing Proxmox cluster, never on a GX10 — a monitor
   sharing a failure domain with what it monitors loses exactly the history you
   need when a node dies. Also keeps all 3 GX10s interchangeable and keeps
   `cloudflared` off the inference hardware. See
   [deployment.md](deployment.md#central-stack--a-dedicated-proxmox-vm-settled).
3. ~~**`dcgm-exporter` vs. `dgx-spark-prometheus`.**~~ **Deferred to Phase 4**
   rather than decided: `spark-dash-agent` reads NVML directly for the basics,
   and DCGM's headline advantage (GPU memory) is what unified memory breaks on
   GB10. Revisit only if deep profiling telemetry turns out to be wanted.
4. **Cross-node orchestration** (still Compose-per-host vs. eventually Swarm/k8s)
   is out of scope for *this* project but affects how much Prometheus service
   discovery needs to do — revisit if orchestration changes.
