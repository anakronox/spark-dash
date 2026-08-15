# Roadmap

Phased so that we get a useful dashboard on the single existing node quickly,
before the other 2 GX10 units even arrive.

**Status as of 2026-08-15.** Phases 1–3 are substantially shipped and running on
`sparky`. Phase 2 is blocked only on hardware. Checkboxes below were trued up
against the deployed system, not against memory — items marked *(unverified)*
are ones that depend on infrastructure outside this repo and were left
unchecked rather than guessed at.

## Phase 0 — Project setup

- [x] Requirements, architecture, metrics, deployment, and app-design docs.
- [ ] Mirror phases below as Forgejo issues/milestones for tracking.
  *(unverified — not visible from the repo.)*

## Phase 1 — Single-node MVP

Goal: something real running on the existing GX10, informative for day-to-day use.

- [x] Scaffold the monorepo layout (`common/`, `agent/`, `backend/`,
  `frontend/`, `deploy/`) — see [app-design.md](app-design.md#repo-layout).
- [x] Provision the monitoring VM on Proxmox — running at `192.168.50.156`
  with Prometheus, Alertmanager and the backend.
- [x] Write the per-node Compose file (`node-exporter` + `spark-dash-agent`).
  **Deviation from the original plan:** the stack deliberately does *not* join
  the inference stack's Docker network. The agent reaches routers and vLLM by
  host address instead, which keeps the stack free of an external-network
  dependency that has to exist before it can start. See the note at the bottom
  of [../deploy/node/compose.yaml](../deploy/node/compose.yaml).
- [x] Build `spark-dash-agent` with its collector modules: `gpu`, `memory`
  (UMA-correct), `psi`, `clock`, `llama_router`, plus `network`/`rdma` added
  later. `spark_hwmon` remains deliberately descoped — see
  [deployment.md](deployment.md#spark_hwmon--evaluated-deliberately-descoped).
- [x] Validate the agent's memory numbers against real hardware.
  `./scripts/validate-on-gx10.sh` passes 11/11 on `sparky`: unified memory
  detected, total agrees with `/proc/meminfo` within 2%, power conversion
  plausible, clock load-gating correct, host PID namespace visible.
- [ ] Confirm vLLM containers expose `/metrics` and add them as Prometheus
  scrape targets. **Not done.** The `vllm` job exists in `prometheus.yml` but
  `targets/static/vllm.yml` is absent and `VLLM_URLS` is unset, so no vLLM
  instance is scraped anywhere.
- [x] Stand up Prometheus on the monitoring VM, scraping the GX10 over the LAN.
- [x] Backend API (Python 3.12 + FastAPI): `/api/nodes`, `/api/history`,
  `/api/models`, `/api/models/timeline`, `/api/cluster/summary`, `/api/alerts`,
  `/health`, and the `/ws/live` WebSocket.
- [x] Frontend MVP (Svelte 5 + Vite + TS, uPlot): live view with stat tiles,
  process table, models table, trends, network panel, alerts.

## Phase 2 — Multi-node cluster

Triggered by the 2 additional GX10 units arriving. **Blocked on hardware** —
only `sparky` exists today, and it is the only scrape target.

- [ ] Roll out the same per-node stack to nodes 2 and 3.
- [x] Move Prometheus target list to file-based service discovery. **Done** —
  `prometheus.yml` uses `file_sd_configs` against
  `targets/generated/{agents,node-exporters}.yml`, rendered by the backend from
  `SPARK_NODES` with a 30s refresh. Adding a node needs no Prometheus config
  change and no restart.
- [x] Extend backend/frontend to aggregate across nodes — `/api/cluster/summary`
  and the per-node health cards exist and handle grouping.
- [ ] Point the central Prometheus at all 3 nodes.

> **Note for the rollout:** a stack clone created before `79470ea` still tracks
> `.env`, and pulling past that commit fails with "local changes would be
> overwritten" as soon as anyone pins `AGENT_IMAGE`. Reconcile with: copy `.env`
> aside, `git checkout -- .env`, pull, copy back — verifying by checksum. Done
> on `sparky` 2026-08-15; nodes 2 and 3 will need the same if they get clones
> from before that commit.

## Phase 3 — Remote access & hardening

- [ ] Confirm dashboard works through the Cloudflare Tunnel + Google OAuth path.
  *(unverified.)*
- [ ] (Optional, defense-in-depth) Validate `Cf-Access-Jwt-Assertion` in the
  backend.
- [ ] Set real Prometheus retention based on observed disk usage. **Now
  answerable** — see [Next up / A7](#a--alerting-correctness) for the measured
  numbers.
- [x] Add a meaningful `/health` endpoint to the backend — it verifies node
  reachability rather than just returning 200. Pointing UptimeKuma at it is
  *(unverified.)*
- [x] Basic alerting for the things that matter at 2am — Alertmanager with 12
  rules delivering to ntfy. **But see Next up / A: the rules have real gaps,
  and the thresholds disagree with the agent's own health model.**

## Next up (prioritized)

The three workstreams below came out of an audit of the running system on
2026-08-15. Ordered by whether they fix something currently *wrong* versus add
something currently *missing*.

### A — Alerting correctness

The highest-value work, because parts of it are wrong today rather than absent.

The root cause of the temperature problem is in
[health.py](../common/src/spark_dash_common/health.py): CPU and GPU are judged
against **the same** `TempThresholds` pair.

```python
findings.extend(_temp_findings("GPU", gpu.temp_c, temps))
findings.extend(_temp_findings("CPU", cpu_temp_c, temps))
```

Two components with very different normal ranges share one set of bands, so
neither can be tuned without breaking the other.
[thresholds.py](../common/src/spark_dash_common/thresholds.py) already documents
the consequence — the GX10 was observed at 84°C during routine ComfyUI
generation at 96% utilization *without* throttling, so 80°C is a normal working
temperature on this hardware. Observed live on 2026-08-15: GPU 82°C and CPU
92°C under load, health `critical`, clock state `PASS`, no alert firing. That is
exactly the predicted false alarm.

- [ ] **A1.** Split `TempThresholds` into separate GPU and CPU bands. This is
  the root fix; everything else in the temperature story is downstream of it.
- [ ] **A2.** Surface `TEMP_WARNING_C` / `TEMP_CRITICAL_C` in
  [../deploy/node/.env.example](../deploy/node/.env.example). The per-node
  override already exists in `config.py` but appears in no template, which is
  why `sparky` is still running the noisy default.
- [ ] **A3.** Add CPU temperature alert rules. `sparkdash_cpu_temperature_celsius`
  is exported and feeds the health model, but has **no rule at all**, while GPU
  has two (`>88` warning, `>94` critical).
- [ ] **A4.** Link-down and RDMA alerts. `sparkdash_network_up`,
  `sparkdash_rdma_port_active` and the `*_errors_total` counters all exist and
  nothing watches them — and for a pair doing distributed inference over RoCE, a
  link dropping is precisely the 2am failure this was built for.
  **Design constraint:** `network_up == 0` is *normal* for the unused f1 ports
  and for wifi, so the rule must key on interfaces that were previously up
  rather than on any down interface.
- [ ] **A5.** Revisit `MemoryHighWithSwap`. It requires >85% memory, so the
  6.1 GiB of swap observed at 37% memory usage is currently invisible.
- [ ] **A6.** Node disk-space alert from node-exporter. Named in the original
  Phase 3 list and never built; `PrometheusStorageFillingUp` covers only the
  monitoring VM.
- [ ] **A7.** Raise retention. Measured on 2026-08-15: 227 samples/sec,
  ~25 MB/day/node, 35 MB on disk for the first ~33 hours. Three nodes ≈
  75 MB/day → 30d ≈ 2 GB, a full year ≈ 25 GB. The current 30d setting is
  roughly 50× more conservative than the disk justifies.

### B — Per-workload GPU memory history

Export `sparkdash_gpu_process_memory_bytes{node, runtime}` — process GPU memory
**summed by runtime**, never per-pid (pid churn would make cardinality
unbounded; by runtime it is ~7 series per node).

This is what makes the unified-pool problem answerable after the fact. The split
is visible live and then lost — on 2026-08-15 that was 26.4 GiB of llama.cpp
against 4.7 GiB of ComfyUI competing for one pool, with no way to reconstruct it
later. Given that non-LLM GPU workloads are real capacity pressure on GB10 and
cannot be isolated the way separate VRAM would be, this is closer to core
telemetry than to a nice-to-have.

- [ ] **B1.** Aggregate `processes` by `runtime` and export as a gauge.
- [ ] **B2.** Optional companion `sparkdash_gpu_process_count{node, runtime}`.
- [ ] **B3.** Check whether the `model` field on processes is ever populated
  before considering it as a label — it is empty for all processes on `sparky`
  today, so it is not usable as one yet.

### C — Multi-node readiness

Work that should land *before* nodes 2 and 3 arrive, so the rollout doesn't
have to double as a debugging session.

- [ ] **C1.** Expose the node's group as a Prometheus label. The agent doesn't
  know its own group (`group` is null in the snapshot); grouping lives in
  `SPARK_NODES` on the backend. Cleanest path is for the backend to emit it as a
  file-SD label in `agents.yml` so it attaches at scrape time. This is what makes
  the *sum within a group, never across groups* capacity rule expressible in
  PromQL. `honor_labels: true` is safe here because the agent exposes no
  conflicting `group` label.
- [ ] **C2.** `sparkdash_agent_build_info{node, build}` plus an alert on
  `count(count by (build)(sparkdash_agent_build_info)) > 1`. Commit `b9ce1f1`
  built the dashboard half of this; the metric turns build skew from something
  you have to notice into something that pages.

## Phase 4 — Polish / nice-to-haves (unscheduled)

- [x] Historical trend views — token throughput and GPU utilization over time.
- [x] Router swap event log/timeline — `/api/models/timeline` and the swap
  timeline component.
- [ ] Optional Grafana pointed at the same Prometheus for ad hoc exploration.
- [ ] Job-level drill-down (per-request tracing), if useful.
- [ ] Evaluate `dcgm-exporter` / `dgx-spark-prometheus` for deep GPU profiling —
  still deferred, and DCGM's memory reporting is broken by GB10 unified memory
  anyway. If `dgx-spark-prometheus` is adopted, package it in our own Dockerfile
  rather than its upstream systemd install path.

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
   need when a node dies. See
   [deployment.md](deployment.md#central-stack--a-dedicated-proxmox-vm-settled).
3. ~~**`dcgm-exporter` vs. `dgx-spark-prometheus`.**~~ **Deferred to Phase 4**
   rather than decided: `spark-dash-agent` reads NVML directly for the basics,
   and DCGM's headline advantage (GPU memory) is what unified memory breaks on
   GB10.
4. **Cross-node orchestration** (still Compose-per-host vs. eventually Swarm/k8s)
   is out of scope for *this* project but affects how much Prometheus service
   discovery needs to do — revisit if orchestration changes.
5. **Should alert rules derive from the agent's health model, or stay
   independent?** Today they are two separate opinions about what "bad" means,
   which is how the dashboard can read `critical` while nothing pages. Either
   alert off `sparkdash_node_health{state="critical"}` and make the agent the
   single source of truth, or keep them separate with the rationale written
   down — a dashboard tile may be twitchy, a page should not be. Leaning
   separate-but-documented, since paging thresholds genuinely should be laxer.
   Settle this before doing A1–A3, because it decides whether the thresholds
   live in one place or two.
6. **Dockhand is not yet orchestrated.** Deploys are currently manual — `git
   pull` in the stack dir, then `docker compose up -d`. The docs and
   `sync-stack-repos.sh` describe the intended end state ("Dockhand redeploys on
   the git change it just saw"), so a push to a stack repo changes nothing on
   any host until someone pulls. Worth closing before the 3-node rollout, when
   doing it by hand stops being cheap.
7. **Should there be a database?** **Leaning no for metrics, yes eventually for
   events — but only after A7 and B.**

   The question comes up naturally ("wouldn't a database help with trends and
   fault analysis?") and deserves a written answer, because the instinct is
   reasonable and the answer is non-obvious.

   **There already is one.** Prometheus is a time-series database, and trends
   plus fault analysis are exactly its job. A general-purpose database alongside
   it would duplicate a purpose-built store with a worse one: no PromQL, no
   rate/aggregation primitives, no retention management, and downsampling done
   by hand. For the "track trends over a longer window" motivation specifically,
   **A7 is the cheap answer** — one line, ~25 GB for a year.

   **Three things Prometheus genuinely handles badly**, though:

   - *High-cardinality detail.* Per-pid series would blow up cardinality, which
     is why B aggregates by runtime. The aggregate trend gets recorded; the
     per-process detail is still lost.
   - *Discrete events with context.* "At 03:14 the agent restarted on build X,
     model Y was evicted, the router returned this error." The timeline endpoint
     already reconstructs transitions from `sparkdash_llama_model_state`, but
     that is a lossy reconstruction of something that was really a record.
   - *Anything meant to outlive retention.* Alert history is the clear case —
     Alertmanager keeps no long history, so "how often did this fire last
     quarter?" is unanswerable today.

   **If we build anything, build event-triggered snapshot capture** — not
   continuous storage. On a transition into `critical`, write the *full*
   snapshot as one row: every process with pids and GPU memory, complete router
   state, PSI, temperatures. A few KB per event, a handful of events a week,
   keeps forever because the volume is trivial. That captures the forensic
   detail Prometheus cannot hold, at the only moment anyone wants it.

   This gap is real and was observed directly: when `sparky` went `critical` on
   2026-08-15, the 26.4 GiB llama.cpp / 4.7 GiB ComfyUI split was visible only
   because someone was watching live. An hour later it was unrecoverable.

   SQLite is the right size — one file under `DATA_ROOT`, no new container, no
   new service to monitor. Postgres or Timescale is operational overhead beyond
   what a 3-node homelab earns back. Prometheus remote-write to
   VictoriaMetrics/Thanos solves long retention properly but is aimed at a scale
   where 25 GB/year isn't already trivial.

   **The cost is real and should not be waved away:** it makes the backend
   stateful, which it currently is not. Today that container can be destroyed
   and recreated freely, and [../deploy/node/README.md](../deploy/node/README.md)
   leans on exactly that property. A database means a schema, migrations,
   corruption modes, and a second thing in the backup set — softened only by the
   monitoring VM already carrying the TSDB, so backup discipline isn't starting
   from zero.

   **Sequence: A7, then B, then reassess.** B may satisfy enough of the
   fault-analysis need that the snapshot log stops feeling necessary — and that
   is the outcome to hope for, since it costs nothing to maintain.
