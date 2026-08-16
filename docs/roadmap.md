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
- [x] Confirm vLLM containers expose `/metrics` and add them as Prometheus
  scrape targets. **Done 2026-08-16** against a real instance on
  `sparky:8120` serving `qwen36-35b-heretic`.

  All five metric names the collector expects were verified present on that
  build before anything was wired, so no collector change was needed. It
  exposes 76 `vllm:` families in total — spec-decode counters and MFU
  estimates among them — so there is more to read here than we currently do.

  Both paths are configured because they answer different questions: the agent
  polls it for the live view (`VLLM_URLS`), and Prometheus scrapes it directly
  for history (`targets/vllm.yml`, labelled `node: sparky` so the series join
  against that node's GPU and memory metrics). Target came up healthy with 426
  series and no Prometheus restart — `file_sd` picked it up on its own refresh.
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

> **Durability is handled at the hypervisor, not here.** The monitoring VM sits
> on Proxmox with ZFS replication to two additional nodes, and backup jobs run
> at the Proxmox Datacenter level — implicit unless a guest is explicitly
> excluded. So there is deliberately no in-guest backup job for the Prometheus
> TSDB, and adding one would duplicate work already done a layer down. Recorded
> because the absence of a visible backup job inside the VM otherwise reads as a
> gap; it isn't one.

- [ ] Confirm dashboard works through the Cloudflare Tunnel + Google OAuth path.
  *(unverified.)*
- [ ] (Optional, defense-in-depth) Validate `Cf-Access-Jwt-Assertion` in the
  backend.
- [x] Set real Prometheus retention based on observed disk usage. **Done** —
  raised 30d → 180d from measured ingest, deployed and confirmed live. See
  [Next up / A7](#a--alerting-correctness) for the numbers.
- [x] Add a meaningful `/health` endpoint to the backend — it verifies node
  reachability rather than just returning 200. **UptimeKuma monitors both
  `:8080` (backend) and `:9090` (Prometheus) on `192.168.50.156`**, confirmed
  2026-08-16, so the "who monitors the monitor" gap is closed.
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
- [x] **A4.** Link-down and RDMA alerts: `NetworkLinkDown`, `RdmaPortDown`,
  `NetworkErrorsRising`, `RdmaErrorsRising`.

  The design constraint is solved with `max_over_time(...[7d]) == 1` — "has been
  up at some point" — which separates a link that *failed* from one that was
  never in service, with no allowlist to maintain and no manual step when a new
  port is cabled. Verified against live data: the three never-used interfaces
  (both ConnectX-7 f1 ports and wifi) are excluded automatically, and the three
  working links are exactly the set being watched.

  Known behaviour: a link down longer than the window stops alerting, since it
  is no longer "recently up". 7d is long enough that this only happens for a
  deliberate decommission, which is what silencing is for.
- [x] **A5 (part 1).** Classify PSI on `avg60` rather than `avg10`.

  **The original note here was wrong and is worth recording as such.** It said
  6.1 GiB of swap at 37% memory was "invisible", implying a missed alert.
  Measurement said otherwise: swap sat at 6.13–6.40 GiB *unchanged* across 24h
  while memory never passed 42%. That's **stale swap** — pages evicted in some
  past squeeze that nothing has touched since, because Linux never faults them
  back proactively. Alerting on it would have been a false positive.

  What the data did reveal was worse. Real pressure occurred — `full_avg10`
  peaked at **51%**, twice its critical band — at **42% memory**, so any rule
  gated on memory percentage could never have caught it. And PSI, which did
  catch it, was classified on `avg10`:

  | band | on `avg10` (24h) | on `avg60` (same period) |
  |---|---|---|
  | MOD | 405 s | ~50 min |
  | HIGH | **45 s** | ~36 min |
  | CRITICAL | **60 s** | **409 s** |

  Alert rules wait 2–5 minutes. Bands that never persisted past a minute meant
  **every alert sat in `pending` and none ever fired.** Moving to `avg60` makes
  the existing `for:` durations correct without touching them — smoothing fixed
  the alerting gap as a side effect of fixing the flicker.

  Note the bands are now effectively stricter, since smoothing halves the peaks
  (24h maxima: 52% on `avg10` vs 28% on `avg60`). `some_critical` at 50 is
  essentially unreachable; CRITICAL is now reached via `full_critical`.

- [x] **A5 (part 2).** Fixed the memory rule now that its premise is understood.
  - Drop the `swap_used > 0` conjunct from `MemoryHighWithSwap`. Stale swap
    makes it ~always true, so it's a no-op that makes the rule look more
    specific than it is.
  - Rename it to what it actually measures: **capacity**, not pressure. "Above
    85% memory, little room to load another model" is a useful alert; it just
    isn't about swapping, and pressure is PSI's job.
  - Add a swap-**I/O** rule for genuine thrash: `rate(node_vmstat_pswpout[5m])`
    sustained. The counters exist and work (24h peak: 18.6 pages/s swap-in). On
    a unified-memory box, sustained swap-out means model weights heading to
    disk — a distinct condition PSI describes only indirectly.
- [x] **A6.** Node disk alerts: `NodeDiskFillingUp` (predict_linear, a week
  ahead) and `NodeDiskLow` (a 5% floor, for fills too slow to project). Scoped
  to `fstype=~"ext4|xfs|btrfs"`, which excludes the GX10's two ~19TB NFS mounts
  and `/boot/efi` — the latter is small by design and would read as nearly full
  forever on a percentage rule.

  **This uncovered a worse problem than the missing rule.**
  `PrometheusStorageFillingUp` was supposed to cover the monitoring VM and did
  not: node-exporter ran only on the GX10, so its bare `on()` join compared the
  TSDB against 85% of the *GX10's* 3.6 TB root filesystem — about 3.1 TB, on a
  50 GB disk. It could never have fired. Fixed by running node-exporter on the
  monitoring VM under its own job name (`node-exporter-central`) and pinning the
  rule to it. The VM had no host metrics of any kind before this.
- [x] **A8.** The threshold was calibrated against the wrong reference.
  `throttle_threshold_mhz` derived from `max_sm_clock` (**3003 MHz**), which
  three days of measurement show the GB10 never approaches — the clock stayed
  between **2359 and 2483 MHz**, averaging 2406, and doesn't drop at idle
  either.

  NVML has the right number: `nvmlDeviceGetApplicationsClock` reports
  **2418 MHz**, the clock the GPU targets for compute, which the observed range
  brackets almost exactly. The reference is now that.

  | NVML value | GB10 | what it is |
  |---|---|---|
  | `max_sm_clock` | 3003 MHz | boost ceiling, never approached |
  | `ApplicationsClock(SM)` | **2418 MHz** | what it targets — matches reality |
  | observed range (3d) | 2359–2483 MHz | 97.6%–102.7% of target |

  The old derivation gave 1502 MHz and the new one gives 1400 (the field floor,
  since 2418 × 0.5 = 1209 falls below it). Nearly the same number — **it landed
  close by luck, not calibration.** On a part whose boost ceiling sits further
  from its applications clock, the same arithmetic would have been well off.

  Also adds `GpuClockBelowTarget` for the gap `THROTTLED` is too coarse to see.
  Firing below ~1400 MHz means a 42% loss before anything is said; the new rule
  warns at a sustained 85% of target under load. Deliberately **warning**, not
  critical: the band between normal and known-degraded has never been observed
  here, so it reports something unusual rather than asserting a fault.
  `sparkdash_gpu_clock_target_mhz` is exported so the rule compares against the
  node's own target, the same pattern as the temperature bands.
- [x] **A7.** Raised retention 30d → **180d**. Confirmed against a second
  measurement on 2026-08-16: 60 MB of TSDB after ~2.5 days ≈ 24 MB/day with one
  node scraped. Most of that is per-node, so three GX10s land near 65 MB/day →
  180d ≈ 12 GB, 365d ≈ 24 GB.

  180d rather than a year because the VM's disk is 50 GB with ~43 GB free and
  Docker images share it, so 24 GB would be over half the remaining space.
  Erring long is still the right instinct — raising retention later does not
  recover deleted data, so being too short costs permanently while being too
  long costs a disk alert — and both disk rules now watch this host properly.

### D — Alert history view — **shipped 2026-08-16**

A fly-out showing both what is firing now and what has fired before, read back
out of Prometheus rather than stored separately.

Confirmed against real data on first run: **14 episodes in 7 days, 0 fired, 14
pending only.** The prediction below was exactly right, which is the strongest
argument for having built it — that state is invisible in every other view.

**The data already exists.** Prometheus records `ALERTS{alertname, alertstate,
node, severity}` for every pending or firing alert, plus `ALERTS_FOR_STATE`
carrying when it began. With retention at 180d that is real history, and it
reaches back as far as retention does rather than starting from whenever this
ships — the same property that makes the model timeline worth having.

**But it will launch empty, and that is the interesting part.** Every `ALERTS`
series in the last 7 days is `alertstate="pending"`. **Nothing has ever
reached `firing`** — because the PSI rules had `for:` windows longer than the
events they watched and the temperature rules were miscalibrated, both fixed in
A1–A5. So the view must treat **pending episodes as first-class**, not filter
them out as noise. "This nearly fired six times and never crossed the line" is
precisely the signal that would have exposed the `for:` bug months before it
was found by hand.

**Where it goes.** Not a tab: alert history is low-frequency reference data, and
a top-level tab implies parity with the live dashboard while putting the live
view behind a click. Not an inline disclosure either — the alerts region sits
above everything, so expanding it reflows the whole page each time you glance at
history.

A fly-out overlays instead, leaving the layout beneath untouched, and has room
for a real episode list.

**The constraint that shapes it:** *firing alerts must stay visible with no
interaction at all.* If the fly-out became the only route to discovering
something is wrong, that is a regression for a monitoring dashboard. So the
existing inline banner is untouched and the fly-out is purely additive. It also
fills a real gap — `Alerts.svelte` renders nothing when quiet, so today there is
no affordance to click when all is well.

- [x] **D1.** Backend `alert_history.py`, modeled closely on `timeline.py`:
  reconstruct episodes from `ALERTS` via `query_range`. Same shape of problem —
  a series that exists while active and vanishes when it isn't — so the
  gap-detection approach carries over.
- [x] **D2.** `/api/alerts/history` endpoint, with a range parameter.
- [x] **D3.** Header trigger in the right-hand cluster beside connection state
  and theme: understated when quiet, severity-coloured badge with a count when
  firing.
- [x] **D4.** The fly-out itself, as a native `<dialog>` + `showModal()` —
  focus trapping, Escape, backdrop and focus restore come from the platform
  rather than being hand-rolled, and it is supported in Safari 15.4+. Styled
  right-anchored and full-height, full-width on narrow screens. Note the
  codebase has **no overlay pattern yet** (the only `z-index` anywhere is `5`,
  on the drag lift), so this establishes one — an argument for the primitive
  over inventing a layer system.
- [x] **D5.** Extract the alert-row markup into a shared snippet so the banner
  and the fly-out cannot drift apart.

**Traps to design around, not discover:**

- **Prometheus restarts fragment episodes.** `ALERTS` vanishes on restart and
  the alert re-enters `pending` afterwards — observed repeatedly during the
  2026-08-16 deploys. Without gap handling, one long episode reports as several.
- **A missing series is ambiguous:** it means either "not active" or
  "Prometheus was down". Needs an `up`-based check or a gap threshold, or the
  view invents episodes that never happened.
- **`pending` → `firing` is one episode, not two.** Duration should measure from
  pending-start with firing marked as a transition inside it.
- **Don't poll history.** Fetch on open and on range change only; current alerts
  stay on the existing 30s cadence. A fly-out re-running `query_range` every 30s
  while open is pure waste.
- **Design the empty state deliberately.** "No alert has fired in 7 days" plus
  the pending count is the state that will be seen most, not a degenerate case.

### E — More signal, and correlating it

Came out of asking what would actually help diagnose a trend on a GB10, rather
than what is easy to add.

**Closed question: memory bandwidth is not reachable via NVML on GB10.**
Measured 2026-08-16 — `nvmlDeviceGetUtilizationRates().memory` reads **0% while
the GPU is at 96%**, and per-process `memUtil` is 0 as well. The discrete-GPU
notion of framebuffer-controller activity does not apply to a unified LPDDR5x
pool, the same root cause that makes `nvmlDeviceGetMemoryInfo` report the wrong
size here.

This matters more than a missing metric usually would: on a unified-memory part
bandwidth is plausibly the real bottleneck, and we cannot see it. Everything
below works around that rather than closing it. The only remaining route is DCGM
profiling counters (`DCGM_FI_PROF_DRAM_ACTIVE`), which is a timeboxed spike with
an uncertain answer, not a plan item. **Do not propose NVML for this again.**

**There are 748 distinct metrics in the TSDB and the dashboard reads a
fraction.** Three are already collected by node-exporter and worth surfacing:

- [ ] **E1.** CPU and I/O pressure. `node_pressure_cpu_*` and
  `node_pressure_io_*` are already there; only *memory* PSI is surfaced. "Slow"
  has at least three distinct causes and we currently distinguish one.
- [ ] **E2.** CPU frequency (`node_cpu_scaling_frequency_hertz`, 3.35 GHz
  observed). Grace cores throttling would slow prompt processing invisibly —
  the CPU-side equivalent of the GPU clock check A8 fixed.
- [ ] **E3.** Disk saturation (`node_disk_io_time_seconds_total`, 2 disks).
  Explains a slow cold start: weights coming off disk.

**The correlation layer** — the more valuable half, and mostly assembly of
pieces that already exist:

- [x] **E4.** Multi-metric history: selectable metrics on **one plot against a
  fixed 0/25/50/75/100% axis**, each with its own colour.

  These span %, °C, W, MHz and tok/s, so they cannot share a raw axis — and a
  second y-axis is the single most common charting mistake, because two scales
  let a chart imply a correlation purely by where the crossings land. Each
  series is instead normalised against a **fixed** ceiling (100°C, 300W,
  3003MHz), never the window's own maximum, which would rescale the line every
  time the range changed.

  The absolute reading is not lost: hovering reports the real value in its own
  unit, shown **on the metric chips** rather than in a separate legend. The
  chips already carry the colour and name, so they serve as legend and readout
  together — which is what let the chart legend go, and kept the panel compact.

  Colour follows the metric, never its position: verified in-browser that
  removing the first metric leaves every other line's colour unchanged.
- [ ] **E5.** Event annotations on those charts — model swaps, alert episodes,
  agent build changes. Both event sources already exist (`/api/models/timeline`
  and `/api/alerts/history`); this only draws them on the axis they already
  share. A dip then arrives with its candidate explanation attached instead of
  requiring three views and mental alignment.
- [ ] **E6.** Cluster outlier detection, once nodes 2 and 3 land: same model,
  three nodes, one slower. Needs per-node comparison rather than aggregates,
  and the `group` label (C1) is what keeps "compare within the pooled pair, not
  across groups" expressible.

**The question this cluster can now answer that it could not before:**

```promql
sparkdash_gpu_process_sm_percent{runtime="comfyui"}
  and on(node) sparkdash_llama_model_tokens_per_second
```

Is image generation stealing compute from inference? Memory alone showed ComfyUI
as a 4.9 GiB minor tenant while it was taking 75–91% of SM.

**Sequencing note.** Do E1–E3 early even though the views come later: *you
cannot backfill a metric you did not collect*. Same asymmetry as retention —
being late costs permanently, being early costs almost nothing.

### G — Clearing an alarm

Surfaced 2026-08-16 by deliberately stopping the vLLM container: the target
goes down, `PrometheusTargetScrapeFailing` starts counting toward firing, and
there is nothing in the dashboard that says "yes, I did that on purpose".

Three different things hide behind "clear an alarm", and they want different
answers:

- **Temporarily silence** while you work on something. Alertmanager already
  does this and already has a UI for it on `:9093` — LAN-only, which is why
  the README calls it out. The gap is that you have to leave the dashboard.
- **Acknowledge** — keep it visible but mark it as seen, so a second person
  knows it is being handled. Alertmanager has no concept of this.
- **Retire the thing entirely** — a stopped vLLM instance should leave the
  config, not be silenced forever. That is a config edit, not an alarm action,
  and F8's gap detection is the other half of it: the dashboard should notice
  a *configured but absent* server just as it notices an unconfigured one.

**Same read-only tension as F.** Silencing is a write, through the one service
published on the tunnel — though a much narrower primitive than editing
`cluster.yml`, since a silence cannot repoint an agent at anything. Worth
deciding on its own merits rather than inheriting F's answer by default.

- [ ] **G1.** Decide the scope: silence, acknowledge, or neither.
- [ ] **G2.** If silencing: proxy Alertmanager's silence API, LAN-only, with
  the silence author recorded — an unattributed silence is how an alert gets
  lost.
- [ ] **G3.** Detect *configured but absent*: the inverse of F8. A target that
  has been down long enough is either broken or retired, and the dashboard
  should say which it cannot tell.

### F — One server-side cluster config

**The problem.** The cluster is defined in two places that don't know about each
other: `SPARK_NODES` on the monitoring VM (ids, hosts, groups) and each node's
`.env` (its routers, vLLM endpoints, metrics allowlist). That split is the whole
reason the node stack can't be identical across nodes, which in turn is why
Phase 2 needs one orchestration repo per node.

Collapse both into a single server-side file. The node stack then becomes
byte-identical everywhere and one repo serves the cluster.

**The design is constrained by something built on 2026-08-16.** Two approaches
exist and one is ruled out:

- *Backend polls the routers directly.* Removes the URLs from the node
  entirely — and **does not work**. SM-gated metrics scraping (see D/E work)
  decides whether to scrape a model based on NVML per-process utilization, data
  only the agent has. Moving router polling to the backend means either losing
  that gate — and pinning models in memory again — or inventing a round trip to
  ask the agent whether scraping is safe. It also turns N local polls into N
  cross-LAN polls every 2s.
- **Agent fetches its own config from the backend.** Polling stays on the node
  where the NVML data is. The agent already self-identifies from the host's
  hostname, so it can ask what it should be polling. **This is the approach.**

Sketch:

```yaml
# config/cluster.yml on the monitoring VM — the one place the cluster is defined
nodes:
  - id: sparky
    host: 192.168.50.61
    group: null                    # standalone; a group of one
    runtimes:
      llama_routers:
        - url: http://192.168.50.61:8001
          scrape_metrics: true     # today's LLAMA_METRICS_ROUTERS, per router
        - url: http://192.168.50.61:8108
      vllm:
        - http://192.168.50.61:8120/metrics
```

- [ ] **F1.** `config/cluster.yml` plus a parser, superseding `SPARK_NODES`.
  It has to feed Prometheus target rendering too, or there are still two
  sources — so this touches `inventory.py`.
- [ ] **F2.** `GET /api/agent-config?node=<id>` returning that node's runtime
  block.
- [ ] **F3.** Agent fetches its runtime config on startup and refreshes on a
  TTL, replacing `LLAMA_ROUTER_URLS`, `LLAMA_METRICS_ROUTERS` and `VLLM_URLS`.
  The node `.env` shrinks to `LOG_LEVEL` and optional overrides.
- [ ] **F4.** Cache the last-known config to disk. If the backend is
  unreachable at agent startup the node would otherwise report GPU, memory and
  network but no models at all — degrading to *stale* beats degrading to
  *empty*. **This trades the agent's current full autonomy for central
  control, and that trade should be deliberate.**
- [ ] **F5.** Validation: a typo'd port in a central file silently breaks one
  node's model reporting. `/health` should flag nodes whose config names
  endpoints they cannot reach.

**Do this BEFORE nodes 2 and 3 arrive.** Migrating with one node means one
thing to break; migrating with three means the per-node repos exist first and
then have to be unwound.

#### Exposing it in the UI — read-only, decided 2026-08-16

**The dashboard stays read-only.** No config editing, for two reasons that
outweigh the convenience:

1. It is *the only service published through the tunnel*, and being unable to
   change anything is a deliberate security property, not an oversight. A write
   path would let a compromise of that path repoint agents at arbitrary URLs —
   the agent fetches whatever appears in `llama_routers`, which is a
   request-forgery primitive aimed at the LAN.
2. `cluster.yml` belongs in git. A UI that writes it either bypasses git —
   recreating exactly the repo-versus-live-file drift that cost most of
   2026-08-16 — or needs git credentials inside the backend.

Most of the value here is not editing anyway; it is closing the loop between
what is configured and what is actually happening.

- [ ] **F6.** Cluster panel: each node, its group, its configured runtimes, and
  **whether the agent has actually fetched that config, with a timestamp**.
  Answers "did my edit reach spark3?", which today needs an SSH session.
- [ ] **F7.** Surface F5's reachability check per endpoint, so a typo'd port
  reads as `spark2 · llama_routers · :8002 — no response` instead of failing
  silently.
- [ ] **F8.** **Gap detection: inference servers observed but not configured.**
  The agent already sees every GPU process and its runtime via NVML, so a
  `VLLM::EngineCore` running on a node with no configured vLLM endpoint is
  detectable — and is precisely the failure where nothing else would be
  noticed. Would have caught the `:8120` container before it was wired up.

  **Match on runtime presence, NOT on ports.** The obvious implementation
  compares observed listening ports against configured ones, and cannot work:
  a process's listening port is not readable across the network namespace
  (established while resolving vLLM model attribution). The sound rule is
  coarser — flag when a runtime is observed on a node that has *zero*
  configured endpoints of that type. That catches the completely-unmonitored
  case, which is the one that matters, and avoids false positives from a
  single instance spawning several engine processes.
- [ ] **F9.** Copy-YAML affordance for an unconfigured server: generate the
  block to paste into `cluster.yml`. Most of the convenience of editing with
  no write path.

If editing is ever wanted, the honest form is to split the surface — writes
refused for requests arriving through the tunnel — and have the backend commit
to git rather than write the file. That is real work and it weakens a property
that was built on purpose.

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

- [x] **C1.** Expose the node's group as a Prometheus label.

  **The render half already existed** — `render_file_sd` has been writing
  `group` into both target files all along, so the mechanism proposed here was
  already built. What was missing was the other direction and the documentation.

  **`parse_file_sd` dropped it.** The fallback path (reading target files by
  hand rather than from `SPARK_NODES`) parsed `node` and silently ignored
  `group`, so clustered nodes reparsed as standalone. That fails in the
  dangerous direction: without grouping, memory stops being pooled, the group's
  capacity is under-reported, and a model that would fit looks like it won't.
  The existing round-trip test missed it because its fixture had no groups.

  Documented the aggregation patterns in
  [metrics.md](metrics.md#the-group-label-and-why-totals-are-usually-wrong),
  including the two traps: a standalone node carries *no* `group` label (an
  empty one would create a phantom group), and `sum` without `by (group)` reads
  as cluster capacity while describing capacity that doesn't exist.

  **Not yet verified end-to-end**, and cannot be until a grouped node exists:
  `sparky` is standalone, so no `group` label is currently emitted at all. The
  label should survive `honor_labels: true` on the agent job, since that setting
  governs only labels the target itself exposes and the agent exposes no
  `group` — but that is reasoning, not a measurement. Confirm when node 2 or 3
  arrives.
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

   **Docs updated 2026-08-16.** Both stack READMEs, both `.env.example` files,
   `publish-images.sh` and `sync-stack-repos.sh` now describe the manual
   rollout that is true *today* and the `:latest` steady state that follows,
   rather than asserting either as the whole truth. They previously assumed a
   git change was the only deploy trigger.

   The distinction is kept explicit rather than flipped, because a doc
   describing a system that does not exist yet would mislead anyone deploying
   this week — which is everyone, until Dockhand is actually driving it.

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
