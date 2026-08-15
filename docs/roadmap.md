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
  scrape targets. **Not done**, but the scaffolding is in place: the `vllm` job
  exists in `prometheus.yml` and `deploy/central/targets/vllm.yml` is tracked,
  documented, and deliberately empty (`[]`) pending real instances. `VLLM_URLS`
  is also unset, so nothing vLLM-shaped is scraped anywhere yet. All that's
  missing is target entries.
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

**A1–A3 are done (2026-08-15).** The root cause was in
[health.py](../common/src/spark_dash_common/health.py): CPU and GPU were judged
against **the same** `TempThresholds` pair, so neither could be tuned without
breaking the other.

Measuring the hardware settled what the numbers should be. NVML reports, for
this GB10 — none of which `nvidia-smi` will show you, it prints `N/A` for all of
them:

| Threshold | Value | Meaning |
|---|---|---|
| `SLOWDOWN` | **86 °C** | the GPU begins throttling itself |
| `SHUTDOWN` | **90 °C** | it cuts power to survive |
| `GPU_MAX` | 99 °C | spec maximum |

And the CPU's `acpitz` zones all report a `critical` trip at **104 °C**.

Against those, both sets of thresholds were wrong in opposite directions:

- Health called **80 °C** critical — below the point where the GPU is even
  throttling, which is why `sparky` read `critical` while running perfectly.
- `GpuTemperatureCritical` waited for **94 °C** on a part that powers itself off
  at 90 °C. **That rule could never have fired.**
- `GpuTemperatureHigh` at 88 °C only tripped after throttling had already begun.
- The CPU, judged against the GPU's bands, was called critical at 92 °C with
  12 °C of headroom to spare.

- [x] **A1.** Split `TempThresholds` per component, and **derive both from the
  hardware** rather than hardcoding: the GPU's from NVML's slowdown threshold,
  the CPU's from the thermal zone's critical trip. Critical lands *on* slowdown
  (where performance actually degrades); warning gets 4 °C of lead. The CPU's
  bands sit 6 °C and 12 °C below its trip, because a cooling failure ramps fast.
  Falls back to generic constants when a part won't report, and says so via
  `source` so a guess is never mistaken for a measurement.
- [x] **A2.** Documented the overrides in
  [../deploy/node/.env.example](../deploy/node/.env.example) — including that
  they should normally stay unset, now that the values are read from hardware.
  Both halves of a pair are required; half an override is ignored with a warning.
- [x] **A3.** Added `CpuTemperatureHigh` / `CpuTemperatureCritical`, and rewrote
  the GPU pair. **All four now compare against the node's own exported bands**
  (`sparkdash_{gpu,cpu}_temp_{warning,critical}_celsius`) rather than numbers
  typed into `alerts.yml`, so there is one source of truth and the rules
  self-calibrate to whatever silicon is present. `TemperatureBandsNotDerived`
  flags a node running on fallback guesses.
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
- [ ] **A8.** Check what the clock-throttle threshold is calibrated against.
  `throttle_threshold_mhz` derives from `max_sm_clock`, which NVML reports as
  **3003 MHz** — but the GB10 runs 2411 MHz at 96% utilization and 74 °C, with
  clock state `PASS` and no thermal throttling. If 3003 is a boost ceiling this
  part never reaches, the derived threshold is measured against a speed that
  isn't real, and THROTTLED may be over- or under-reported. Noticed while
  measuring the thermal limits; not yet investigated.
- [ ] **A7.** Raise retention. Measured on 2026-08-15: 227 samples/sec,
  ~25 MB/day/node, 35 MB on disk for the first ~33 hours. Three nodes ≈
  75 MB/day → 30d ≈ 2 GB, a full year ≈ 25 GB. The current 30d setting is
  roughly 50× more conservative than the disk justifies.

### B — Per-workload GPU memory history

Export `sparkdash_gpu_process_memory_bytes{node, runtime, model, router}` —
GPU memory grouped by **workload identity**, never by pid. Pid churn would make
cardinality unbounded; the labels below are all bounded by configuration.

This is what makes the unified-pool problem answerable after the fact. The split
is visible live and then lost — on 2026-08-15 that was 26.4 GiB of llama.cpp
against 4.7 GiB of ComfyUI competing for one pool, with no way to reconstruct it
later. Given that non-LLM GPU workloads are real capacity pressure on GB10 and
cannot be isolated the way separate VRAM would be, this is closer to core
telemetry than to a nice-to-have.

**Per-model attribution is reachable, and the join key is exact.** Investigated
on `sparky` 2026-08-15:

- A llama.cpp **router** runs as `llama-server --models-preset ... --models-max N`
  and holds only ~0.17 GiB of overhead. It serves no single model.
- Each loaded model is a **child** process carrying `--alias <name>` in its argv
  — that's where the real memory sits (26.4 GiB for `qwen36-35b`).
- That alias **is** the model id the router reports from `/v1/models`, which is
  already the `model` label on `sparkdash_llama_model_*`. So the new metric joins
  directly against the existing per-model series: "while this model was active,
  how much of the pool did it hold?" becomes one query.
- Where an alias is ambiguous across routers, the router reporting that model as
  `ACTIVE` is the one holding its weights, which resolves it from data already
  collected. ~~The child's `PPid` disambiguates.~~ **Corrected during
  implementation:** `PPid` does identify the parent (child 2581341 → 2447163),
  but that parent listens on a container-internal port, so mapping it back to
  the host-side endpoint would need cross-namespace socket inspection. The
  ACTIVE-state signal costs nothing and covers the realistic case.

`ProcessInfo.model` already exists in the schema and is simply never populated —
`infer_runtime` returns the runtime only, and no argv parsing sets the model.
Nothing new needs reading: `infer_runtime` is already passed the command line, so
the alias is available at the point the runtime is decided. No extra permissions
are involved — `/proc/<pid>/cmdline` is world-readable, unlike `cwd`.

- [x] **B1.** Parse `--alias` from argv and populate `ProcessInfo.model` for
  llama.cpp children. `router` is resolved in `snapshot.py`
  (`resolve_process_routers`) by matching the alias against the model lists the
  `llama_router` collector already holds, preferring the router that has it
  ACTIVE and leaving it unset when genuinely ambiguous.
- [x] **B2.** Export the gauge, grouped by `{node, runtime, model, router}`.
  Non-LLM workloads carry an empty `model`/`router` and aggregate by `runtime`
  alone. Cardinality on `sparky` today: 9 configured models plus a handful of
  runtimes.
- [x] **B3.** Companion `sparkdash_gpu_process_count{node, runtime, model, router}`.
- [x] The live process table shows the model alongside the runtime, since the
  field is populated now rather than always null.

**Known limits, worth stating so they aren't rediscovered:**

- The two ComfyUI instances are **byte-identical in argv** (`python main.py
  --listen 0.0.0.0 --port 8188 ...`), so they cannot be told apart from the
  command line. They aggregate as `comfyui`, which is the honest outcome.
  Separating them would need `cwd`, which needs ptrace access the agent
  deliberately does not have as non-root.
- Router parents appear with `runtime=llama.cpp` and no model. That is correct —
  it is genuine router overhead, and labelling it as a model would be a lie.
- `--alias` is llama.cpp-specific. vLLM would need its own extraction
  (`--served-model-name`), which is not blocking and can follow.

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
- [x] **C2.** `sparkdash_agent_build_info{node, build}` plus the `AgentBuildSkew`
  alert on `count(count by (build)(sparkdash_agent_build_info)) > 1`, held for
  30m so a rollout in progress doesn't page — it catches a node that is *stuck*.
  Commit `b9ce1f1` built the dashboard half; this is the Prometheus half.

  **Load-bearing rather than cosmetic** under the `:latest` decision (see Open
  decisions 6): config no longer records which build is deployed, so this is the
  only historical answer to "what was running when" — and a better one, since it
  records what actually ran rather than what a pin intended. It also stamps any
  metric with its build:

  ```promql
  sparkdash_gpu_utilization_percent
    * on(node) group_left(build) sparkdash_agent_build_info
  ```

  which separates "the GPU numbers changed" from "the agent that measures them
  changed".

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
   independent?** **Settled for temperature (2026-08-15): they share a source.**
   The agent exports the bands it judges itself against
   (`sparkdash_{gpu,cpu}_temp_{warning,critical}_celsius`) and the rules compare
   against those, so the two can no longer disagree — which they did, badly,
   with health calling 80 °C critical while a rule waited for 94 °C on hardware
   that shuts down at 90 °C.

   This turned out better than either original option: the rules are neither
   independent nor a copy of the agent's numbers, they *reference* them, and so
   self-calibrate to whatever hardware reports. The same pattern is available to
   any other threshold that has a hardware-derived answer. The original question
   as posed: Today they are two separate opinions about what "bad" means,
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

   **Planned shape — two phases, decided 2026-08-15:**

   1. Clone the source repo into `/docker/` and pull it freely. Nothing in it is
      ever hand-edited, so a pull can never conflict.
   2. A separate Dockhand orchestration repo holding **only** what Dockhand
      needs to deploy plus what gets hand-modified on the host — `compose.yaml`,
      `.env`, and anything else that a pull would clobber or that would block a
      pull with a conflict.

   The principle worth keeping: *a file that is hand-edited on the host must
   live in exactly one repo — the one nobody pulls over it.* Today's failure was
   precisely this. `.env` lived in a repo designed to be overwritten, so pinning
   `AGENT_IMAGE` on `sparky` turned the next `git pull` into "local changes would
   be overwritten by merge". `sync-stack-repos.sh` already carries a
   copy-aside-and-restore dance for `.env` that exists *only* because of this
   duplication, and it would go away.

   **Who owns `compose.yaml`? Settled: the orchestration repo does.** This
   mirrors the pattern Brian already uses for third-party stacks — take the
   compose file (and `.env` if needed), hand-create them in a Forgejo repo, and
   point Dockhand at that. It's the only workable approach for a public upstream
   you can't sync from, and it generalizes here.

   Consequences, accepted deliberately:

   - `deploy/*/compose.yaml` become **reference templates**, not the source of
     truth. What runs is what's in the orchestration repo, so reading the host
     tells you the truth — the property that was missing when the node README
     described a path that didn't exist.
   - `sync-stack-repos.sh` loses its purpose entirely rather than shrinking. A
     template change has to be applied by hand, which is cheap because there are
     only two stack repos, not one per node.

   **Settled: one orchestration repo per node.** Node `.env` holds
   `LLAMA_ROUTER_URLS` and `VLLM_URLS`, which name *that host's* routers by LAN
   IP (the agent has its own netns, so `localhost` isn't the node) — so the
   values are per-node and a shared tracked `.env` was never going to work.
   Per-node repos make the live config versioned and recoverable, which is worth
   more than one-repo-serves-all.

   This does **not** conflict with cloning the single source repo to every host.
   The two are orthogonal: the source repo holds nothing hand-edited so it pulls
   freely everywhere, and the orchestration repos are never pulled over. No file
   lives in both, which is the whole point.

   It also makes image rollout orchestrated rather than manual: pinning
   `AGENT_IMAGE` becomes a commit in that node's repo, which is exactly the git
   change Dockhand watches for.

   **Build on one node, not all three.** All GX10s are arm64 and the image
   carries nothing node-specific (`NODE_ID` comes from the host's hostname at
   runtime), so `publish-images.sh agent` should run on exactly one of them. Two
   nodes each building and pushing the same tag would leave the second
   overwriting the first with a **different digest under the same tag**, and
   nodes would then run different bytes depending on when they pulled. The
   source clone on the other nodes exists for `validate-on-gx10.sh` and
   diagnostics, not for building.

   **Images track `:latest`, not a pinned sha.** Dockhand is configured per
   managed environment to pull new images once a day in off-hours, so `:latest`
   converges without a git change — which pinning cannot do, since a pinned tag
   only moves when someone edits it.

   This reverses the advice still written in
   [../deploy/node/README.md](../deploy/node/README.md) and printed by
   `publish-images.sh`, both of which assume a git change is the only deploy
   trigger. **Both need updating when this lands.**

   Consequences, accepted:

   - **The build becomes the deploy action.** Pushing to `main` ships nothing;
     images exist only when `publish-images.sh` runs. Running it means "this goes
     live on every node in the environment within 24 hours," which makes the
     publish step weightier than it is today, where a pin edit stood between
     building and running.
   - **Skew is bounded, not eliminated** — nodes in one environment pull
     together, and worst case diverge for under a day rather than indefinitely.
   - **Config no longer records what's running.** `:latest` is not an answer to
     "what was running on the 12th?", which makes **C2 load-bearing rather than
     optional**: `sparkdash_agent_build_info` records what actually ran, is
     queryable historically, and is more honest than a pin, which only ever
     recorded intent.
   - **Pinning survives as the exception path.** When a bad build lands
     overnight, pin the last-good sha in that node's `.env` and commit —
     Dockhand redeploys immediately, no rebuild needed — then unpin once fixed.

   **Config-only changes need a container recreate, not a reload.**
   `prometheus.yml`, `alerts.yml` and `alertmanager.yml` are bind-mounted as
   single files, and a file mount follows the inode. A `git pull` replaces the
   file, so the container keeps reading the old inode and a reload silently
   re-reads stale content — observed 2026-08-15 while deploying C2. If Dockhand
   pulls the orchestration repo and runs a plain `docker compose up -d`, a
   config-only change will **not** take effect, because the compose config
   itself is unchanged. Either Dockhand must `--force-recreate`, or these files
   need to move to a directory mount. Worth settling as part of the migration.

   **Guard against drift instead of syncing.** Three hand-maintained
   `compose.yaml` files can diverge once the orchestration repos are
   authoritative. The replacement for `sync-stack-repos.sh` should be a *drift
   check* that diffs each orchestration repo against `deploy/node/compose.yaml`
   and reports differences — keeping the template honest without reintroducing
   the clobbering this design removes. This also raises the value of **C2**:
   three independently-pinned nodes make build skew routine rather than
   exceptional.
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
