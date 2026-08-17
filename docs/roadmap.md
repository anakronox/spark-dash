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
  `frontend/`, `central/`, `node/`) — see [app-design.md](app-design.md#repo-layout).
- [x] Provision the monitoring VM on Proxmox — running at `192.168.50.156`
  with Prometheus, Alertmanager and the backend.
- [x] Write the per-node Compose file (`node-exporter` + `spark-dash-agent`).
  **Deviation from the original plan:** the stack deliberately does *not* join
  the inference stack's Docker network. The agent reaches routers and vLLM by
  host address instead, which keeps the stack free of an external-network
  dependency that has to exist before it can start. See the note at the bottom
  of [../node/compose.yaml](../node/compose.yaml).
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
  `prometheus.yml` uses `file_sd_configs` against `targets/`, rendered by the
  backend from `cluster.yml` with a 30s refresh. Adding a node needs no Prometheus config
  change and no restart.
- [x] Extend backend/frontend to aggregate across nodes — `/api/cluster/summary`
  and the per-node health cards exist and handle clustering.
- [ ] Point the central Prometheus at all 3 nodes.

> **Note for the rollout:** nodes 2 and 3 need no stack repo and no per-node
> config. Clone `spark-dash-homegrown` into `/docker/`, `cd node`, copy
> `.env.example` to `.env` (only `BACKEND_URL` and the image pin matter), and
> `docker compose up -d`. Then add the node to `central/cluster/cluster.yml` on
> the monitoring VM — that is where its routers, cluster and identity live.
>
> The old hazard here — a stack clone predating `79470ea` still tracking `.env`,
> so pulling past that commit failed with "local changes would be overwritten" —
> is gone with the stack repos themselves.

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
  [../node/.env.example](../node/.env.example) — including that
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

- [x] **E1. CPU and I/O pressure — shipped 2026-08-17.** "Slow" had at least
  three causes and the dashboard could distinguish one. Memory pressure said the
  box was thrashing; a machine stalled on runqueue or on disk looked identical
  to a healthy one.

  Note the smoothing differs from the memory gauge beside it, and that is worth
  knowing when comparing the two: memory PSI is the kernel's own 10-second
  average read by the agent, while these are counters of seconds stalled, so the
  backend takes a rate over a window scaled to the chart's step.
- [x] **E2. CPU frequency — shipped 2026-08-17.** `avg by (node)`, not max:
  throttling shows up as every core dropping together, and a max would be held
  up by whichever core happened to boost — hiding the exact condition this
  exists to catch. Ceiling 3500MHz against 3354MHz observed, so a healthy clock
  sits near the top of the axis rather than at it.
- [x] **E3. Disk saturation — shipped 2026-08-17.** `max by (node)` across
  devices, not avg: saturation is "is any disk pegged", and averaging a busy
  disk against an idle one reports a comfortable 50% for a machine completely
  stalled on one of them.

**Three things E1-E3 turned up, worth keeping:**

- **node_exporter already carries a `node` label**, because the file_sd targets
  are written with one. These slot into the same per-node charts as the agent's
  metrics with no joining at all — which is why this was an afternoon rather
  than a project.
- **node_exporter also runs ON the monitoring VM**, under its own job. Without a
  `job="node-exporter"` filter the VM appears in every chart as a node the
  cluster does not contain, with no card and no colour slot.
- **A fixed rate window is wrong at both ends.** Too long and a 1h chart smooths
  away the spike you opened it for; too short and a 7d chart samples two minutes
  out of every ten and calls it a trend. The window is now four steps wide,
  floored at 1m.

**The metric chips stopped meaning anything and nobody noticed.** Their swatches
were the metric's own hue — correct when every metric was a line on one shared
plot. Since O split them, each metric has its own chart and the lines in it are
coloured by NODE, so a per-metric hue named a colour that appeared nowhere on
the page. Exactly the fault the node legend had. They are now a fill-vs-hollow
state mark, and `metricColor` is deleted.

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
  and the `cluster` label (C1) is what keeps "compare within the pooled
  cluster, not across clusters" expressible.

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

**Resolved 2026-08-16: silencing is allowed.** "Read-only" was always about
not loading, unloading or killing things — not about being unable to manage
alarms. A silence cannot repoint an agent, load a model or touch a process; its
worst case is muted alerts, bounded and behind the same OAuth as the rest of
the page. That is a categorically narrower primitive than editing
`cluster.yml`, which would have been a request-forgery vector into the LAN.

The workflow argument is the decisive one. This box runs experiments — stacks
come up and get torn down constantly, and every teardown leaves a target down
and an alert firing with no way to say "yes, that was me". **An alert you
cannot clear is one you learn to ignore**, which is worse than no alert.

- [x] **G1.** Scope: **silence**, not acknowledge. Alertmanager has silences
  natively; acknowledgement would be a new concept needing its own storage.
- [x] **G2.** Silence and unsilence through the dashboard, proxying
  Alertmanager's API. Three guardrails, all deliberate:
  - **Always bounded, capped at 24h.** The failure mode of silencing is
    forgetting, and a week-long mute set during a five-minute experiment is
    indistinguishable from an outage nobody is watching. Anything needing
    permanent silence should have its target removed from configuration —
    which is the honest fix for a retired stack.
  - **Scoped to the alert instance**, not just `alertname`. Silencing on the
    rule name alone would mute it on every node, so a torn-down stack on one
    box could hide a real failure on another.
  - **Active silences are always visible**, with an undo. A muted alert nobody
    can see is a way to hide problems from yourself.

  Verified end to end against the live `PrometheusTargetScrapeFailing` from the
  deliberately-stopped vLLM container: silence → alert clears → appears under
  Silenced with its scope and time remaining → unsilence → alert returns.

  **Not yet done: per-user attribution.** Silences are recorded as
  `spark-dash` rather than a person, because the backend has no user identity —
  OAuth terminates at the tunnel edge. Closing that needs the
  `Cf-Access-Authenticated-User-Email` header, which is the same work as the
  Phase 3 JWT item.
- [ ] **G3.** Detect *configured but absent*: the inverse of F8. A target that
  has been down long enough is either broken or retired, and the dashboard
  should say which it cannot tell.
- [x] **G0. Inference alerts age out after 24h** (2026-08-16). Brian's
  suggestion, and it turned out to solve most of what G4 was for with **no UI
  write at all** — it is a rule change, not a feature.

  `PrometheusTargetScrapeFailing` is split in two along the same
  environmental/scraped line:

  - **Infrastructure** (`job!~"vllm"`) never ages out. The hardware still
    exists, so a target that stays down is a fault however long it lasts.
  - **Inference** (`job="vllm"`) resolves after 24h of continuous failure,
    via `max_over_time(up[24h]) == 1` — the same idiom `NetworkLinkDown` uses
    to stop never-cabled ports alerting forever.

  **The tradeoff is real and was accepted deliberately:** a genuine crash ages
  out too. Nothing can distinguish "torn down on purpose" from "died and nobody
  noticed" — both are a container that is not there. What keeps it from being
  silent is that **the fact outlives the alert**: G3 should keep reporting a
  configured-but-absent target, so it stops nagging without being forgotten.
  That makes G3 a dependency of this being safe, not an optional extra.

- [ ] **G4.** **Retire a scrape target from the UI — removal only.**
  Still wanted for the case where you want the target *gone* rather than
  quiet — an aged-out alert leaves a dead entry in config that keeps failing
  scrapes and re-fires if the endpoint briefly returns. But G0 removes the
  urgency.

  Silencing is the wrong tool for something never coming back. It hides the
  alert while leaving the dead target in config, so scrapes keep failing and
  the alert returns the moment the silence expires. What is wanted is for the
  target to stop existing.

  **The line is environmental vs. scraped, and it is not arbitrary.** A GPU
  temperature or memory-pressure alert has no "retire" concept — the hardware
  still exists, and being able to delete those alerts would let someone
  permanently blind the dashboard to a real failure. An inference endpoint is
  configuration: it can genuinely be decommissioned. Only scrape targets are
  removable; environmental rules never are.

  **Removal is safe where add and edit are not**, which is the asymmetry that
  makes this workable at all. The objection to config writes was
  request-forgery — adding a URL makes the agent fetch it. Removal cannot
  introduce a fetch destination or repoint anything; its worst case is that
  monitoring stops for something, the same class as a silence and no worse.
  **So: remove from the UI, add and edit in config.**

  **Blocked on where the config lives.** `targets/vllm.yml` is tracked in git,
  so a UI that removes an entry either edits a tracked file — recreating the
  repo-versus-live drift that cost most of 2026-08-16 — or needs git
  credentials in the backend. Neither is acceptable.

  The fix is the pattern already used for `.env`: **live config on the host,
  template in git.** `cluster.yml` (F1) should live gitignored,
  gitignored and backend-owned, with an example committed. The backend then
  renders *all* scrape targets from it — `vllm.yml` becomes generated like
  `agents.yml` and `node-exporters.yml` already are — and UI removal is a
  normal write to a file the backend owns, with no git involved.

  **So G4 depends on F1**, and F1 should place `cluster.yml` somewhere
  gitignored rather than git-tracked, for exactly this reason. It lives in
  `central/cluster/`.

### F — One server-side cluster config

**The problem.** The cluster is defined in two places that don't know about each
other: `SPARK_NODES` on the monitoring VM (ids, hosts, clusters) and each node's
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

As built:

```yaml
# central/cluster/cluster.yml on the monitoring VM — the one place the
# cluster is defined. Identity AND runtimes, so nothing node-specific is left
# anywhere else.
nodes:
  - id: sparky                     # becomes the `node` label on every metric
    host: 192.168.50.61
    # cluster: alpha               # omitted = standalone; a cluster of one
    #                               a NAME, never a count — see cluster.yml.example
    # agent_port: 9500             # defaults
    # node_exporter_port: 9100
    runtimes:
      # Ports, not URLs — `host` above is filled in when this is served to the
      # agent, so a node's address appears exactly once.
      llama_routers:
        - port: 8001
          scrape_metrics: true     # was LLAMA_METRICS_ROUTERS, now per router
        - port: 8108
      vllm:
        - 8120                     # /metrics appended automatically
```

- [x] **F1.** `cluster.yml` plus a parser, superseding `SPARK_NODES`.
  It has to feed Prometheus target rendering too, or there are still two
  sources — so this touches `inventory.py`.

  **Extended 2026-08-16 to define node identity, not just runtimes.** The
  first cut left ids, hosts and clusters in `SPARK_NODES` and put only runtimes
  here, which reproduced the same split it was meant to close — one file
  saying a node exists, another saying what it serves, with nothing keeping
  them agreeing. `cluster.yml` now carries `id`, `host`, `cluster`,
  `agent_port` and `node_exporter_port` alongside `runtimes`, and `Inventory`
  prefers it over `SPARK_NODES`. Adding a node is one entry in one file, and
  the Prometheus target files are rendered from that same entry.

  Fallback order is `cluster.yml` → `SPARK_NODES` → hand-written target
  files. A missing or empty file falls through, so migration is safe in
  either order; a file that exists but does not parse holds the last good
  node list and logs loudly, because silently reverting to a stale
  `SPARK_NODES` would look like nodes vanishing and get the wrong file
  blamed.
- [x] **F2.** `GET /api/agent-config?node=<id>` returning that node's runtime
  block.
- [x] **F3.** Agent fetches its runtime config on startup and refreshes on a
  TTL, replacing `LLAMA_ROUTER_URLS`, `LLAMA_METRICS_ROUTERS` and `VLLM_URLS`.
  The node `.env` shrinks to `LOG_LEVEL` and optional overrides.
- [x] **F4.** Last-known config is kept **in memory**, not on disk. A
  transient backend outage therefore never blanks a node's routers.

  **Disk caching was considered and rejected.** It would close a narrower
  window — an agent restart *during* a backend outage — at the cost of a
  writable volume on every node, and the node stack has no persistent state at
  all today. That property is worth more: it is why the node README can say
  there is nothing to back up. During a backend outage the dashboard is down
  anyway; what is lost is model metrics for a node that also happens to restart
  in that window.

  **Precedence, decided 2026-08-16: central wins where central has an
  opinion.**

  | situation | what the agent uses |
  |---|---|
  | node is in `cluster.yml` | central config; env ignored |
  | node is absent from it | falls back to env |
  | backend unreachable | last known config, else env |

  The middle row is what makes a rollout safe: deploying the agent before
  adding a node to `cluster.yml` would otherwise take its model reporting dark
  the moment it restarts. A node listed **with no runtimes** still overrides
  env — that is an opinion, and it is how removing a router centrally takes
  effect on a node with a stale `.env`.
- [ ] **F5.** Validation: a typo'd port in a central file silently breaks one
  node's model reporting. `/health` should flag nodes whose config names
  endpoints they cannot reach.

  A neighbouring case worth folding in: an env var that is now ignored. The
  compose file no longer requires `SPARK_NODES`, and `cluster.yml` wins
  whenever it lists a node — so a stale value there is silently inert, which
  looks exactly like an edit that "didn't take".

**Migration status: DONE on `sparky`, 2026-08-16.** The backend runs with
`SPARK_NODES` empty in the container and takes its node list, clustering and
runtimes entirely from `cluster.yml`; the node `.env` carries only
`BACKEND_URL` and `LOG_LEVEL`. Both routers still report models, and the
Prometheus target files are rendered from the same entries.

Doing this with one node was the point — migrating with three would have meant
creating the per-node repos first and then unwinding them. Nodes 2 and 3 are
now additions to `cluster.yml` and nothing else.

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

**Refined 2026-08-17 — see workstream L.** "Read-only" is a claim about NODE
DATA, not about the dashboard's own preferences. Theme, layout, section order
and metric selection are client-side and involve no server write at all;
silencing (G) already writes and was justified by being a narrow primitive.
What stays ruled out here is specifically L3: writing cluster membership and
runtime endpoints through the tunnel-published surface, which is where the
request-forgery argument above actually bites.

- [ ] **F6.** Cluster panel: each node, its cluster, its configured runtimes, and
  **whether the agent has actually fetched that config, with a timestamp**.
  Answers "did my edit reach spark3?", which today needs an SSH session.
- [ ] **F7.** Surface F5's reachability check per endpoint, so a typo'd port
  reads as `spark2 · llama_routers · :8002 — no response` instead of failing
  silently.
- [x] **F8.** **Gap detection: inference servers observed but not configured.**
  **Done 2026-08-16, and it needed none of F1–F5** — the agent's snapshot
  already carries both halves, so this works against the current `.env` setup
  and will keep working after the config migration. Reported per node in the
  snapshot, exported as `sparkdash_unmonitored_runtime{node,runtime}` so it is
  alertable and historical rather than only visible while someone is looking,
  surfaced as a dashboard notice, and alerted by
  `UnmonitoredInferenceRuntime` after 30m — long enough that a
  just-started server is not punished for the normal add-it-next workflow.
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

### I — Cluster naming (done 2026-08-16)

- [x] **I1.** `group` renamed to `cluster` throughout — config key, Prometheus
  label, API field, frontend type and UI. Done while no series carried the
  label and no rule depended on it, which made it free; after nodes 2 and 3
  are clustered it would have split grouped history.

  **It stays a STRING, deliberately.** An int with `0` = standalone was
  considered and rejected: the value is rendered as a UI heading and written
  as a Prometheus label, so `cluster="3"` in a 2am alert needs a decoder ring
  that lives nowhere; `0` conflates "standalone" with "cluster zero" when
  omitting the key already says standalone unambiguously; and numbers invite
  renumbering, which silently rewrites the label and splits history.

  Any scalar still works (`cluster: 2` parses to `"2"`), so numbering is
  available without being taught. The real defect was the example name `pair`
  — a name that encodes the size, and therefore wrong the moment a third node
  joins. Examples now use `alpha`.

  `group:` is still read in `cluster.yml` with a loud warning, because
  silently ignoring it would drop a node to standalone and break capacity
  arithmetic in the dangerous direction.

### L — A settings fly-out

**The distinction that makes this coherent:** "read-only" was always a claim
about NODE DATA — the dashboard observes the cluster, it does not drive it.
Configuring the DASHBOARD is a different thing entirely, and conflating the two
is why settings are currently scattered with no home.

They are genuinely scattered today. Four independent `localStorage` keys, each
owned by whatever component happened to need it, each with its own UI:

    spark-dash.theme.v1           a <select> in the header
    spark-dash.section-order.v1   drag handles on each section
    spark-dash.trend-metrics.v1   chips inside the History panel
    (history range)               buttons inside the History panel

Nothing tells you these are settings, that they persist, or where to find them.
K adds a fifth (compact cards) and the problem gets worse from there.

**Tier the scope by what it WRITES**, because the security argument is
completely different per tier and lumping them together is how the wrong thing
gets built:

- [x] **L1 — client-only. No server involvement, no risk.** Shipped
  2026-08-17. `Settings.svelte`, a right-anchored fly-out reusing
  `AlertHistory`'s shell so the two panels behave identically rather than each
  being hand-rolled: `<dialog>` + `showModal()` for focus trapping, Escape,
  backdrop and focus restore.

  Holds theme, section order and collapse state, plus a plain statement that
  preferences live in this browser only — which is worth saying rather than
  leaving to be discovered, since there is no account to sync them to.

  The theme `<select>` left the header for it. The header is the most valuable
  strip on the page and a control touched twice a year should not hold a
  permanent seat there.

  **What deliberately did NOT move:** the History metric chips and range
  buttons. Controls that sit beside the thing they affect belong there — moving
  them here would trade discoverability for a round trip. A settings panel is
  where homeless options live, not a place to collect every control.

  Still to add here as they land: compact cards (K), and a home for anything
  new that has no natural place on the page.
- [ ] **L2 — server state that cannot reach outward.** Silences already live
  here and are already written from the UI (G), which set the test worth
  reusing: a silence *"cannot repoint an agent, load a model or touch a
  process"*. Any candidate for L2 has to pass that same sentence.
- [x] **L3 — cluster membership, editable. Shipped 2026-08-17**, overriding
  F's blanket read-only stance. The distinction that settles it: "read-only" is
  a property of AGENT DATA — the dashboard observes nodes and never drives
  them. `cluster.yml` is the dashboard's OWN configuration, and editing it is
  the same kind of act as silencing an alert (G): it changes what this service
  watches, not what any node does.

  The concern was never "editable"; it was that the agent polls whatever
  appears in `llama_routers`, making an unconstrained write a request-forgery
  primitive aimed at the LAN. That is addressed by narrowing the value space
  rather than by refusing the feature:

  - **The UI edits PORTS, never URLs.** The backend resolves them against the
    node's own host, so a write cannot name an arbitrary address. This is the
    documented normal form anyway — the `port:` shorthand is what keeps a
    node's address appearing exactly once.
  - **Hosts are validated server-side** against RFC1918, loopback and
    link-local; public IP literals are refused. Hostnames pass, because judging
    one means resolving it and that is a worse kind of trust.
  - **Off-node runtimes** (an explicit `url:` pointing elsewhere) are shown but
    not editable, since editing one would mean accepting free text.

  Neither check is the primary control — OAuth at the tunnel edge is — but they
  cost nothing and mean the edge is not the only thing standing there.

  Writes are atomic (temp file in the same directory, then `os.replace`) and
  the result is re-parsed before it lands, because a half-written cluster file
  would take every node dark. The inventory cache is invalidated on save, so
  the change shows immediately rather than looking like it failed for up to the
  30s TTL.

  **Known cost:** the file is serialised from the parsed model, so hand-written
  comments do not survive a UI save. Stated in the file's own header and in the
  panel, with `cluster.yml.example` named as the documented reference.

**L3 needs an answer, not a form.** Options, roughly in order of preference:

  1. **Read-only display of `cluster.yml` plus a copy-to-clipboard snippet.**
     Covers most of the real need — "what is configured, and what do I paste to
     add a node" — with no write path at all. This is F6/F9 already.
  2. **Write, but constrain the value space.** Ports from an allowlist, hosts
     matched against RFC1918, no free-text URLs. Turns an arbitrary-URL
     primitive into a much narrower one.
  3. **Write, but not through the tunnel.** Bind the mutating endpoints to the
     LAN interface only, so the published surface stays read-only. Costs a
     second listener and some compose work; keeps the security property intact
     rather than trading it away.

  Decide this before building L3. The other tiers do not depend on it.

**Alert thresholds are a trap, and the uncertainty about them is correct.**
Workstream A's whole finding was that hardcoded thresholds were wrong and the
right values come FROM THE HARDWARE — NVML's slowdown point, the CPU's critical
trip. A UI that lets someone type 80°C re-introduces exactly the bug that made
`GpuTemperatureCritical` unable to fire. If thresholds become editable at all,
they should be offsets from the derived value ("warn 6°C before slowdown"), not
absolutes — and the derived number must stay visible beside the field.

**Reuse the fly-out shell.** `AlertHistory.svelte` is already a right-anchored
`<dialog>` + `showModal()`, which gets focus trapping, Escape, the backdrop and
focus restore from the platform. Settings should be the same component family,
not a second hand-rolled overlay — and possibly literally the same shell with
tabs, since "alerts" and "settings" are both things you open, act on and
dismiss.

### M — Making the tables usable at scale

**Measured 2026-08-17, at four nodes:** the models table already carries **36
rows, of which 32 are `unloaded`** — noise you scroll past to find the four
that are doing anything. Same ratio at 32 nodes is ~288 rows.

Widening the page also exposed that these tables do not have enough columns to
fill 1491px however they are sized. That is not a CSS problem; it is the
tables being under-specified for the space now available.

- [x] **M1. Sorting AND pagination — shipped everywhere 2026-08-17**, with the
  row cap exposed as a per-section setting. `lib/table.svelte.ts`
  (`TableView`), `SortButton.svelte` and `Pager.svelte`.

  **Sorting now covers all 32 columns across the four tables** — Models, GPU
  processes, and Network's RDMA and interface tables. The header control was
  extracted to `SortButton.svelte` when the second table needed it: the markup
  is trivial, but it carries the interaction contract (descending first, arrow
  on the active column only, three-state cycle) and four copies of that would
  become four subtly different tables.

  **The `negotiated` column is the one that needed thought.** Its value is the
  driver's verbatim string, deliberately — that string IS the diagnosis when a
  ConnectX-7 comes up at 10 Gb/sec instead of 200. But sorting it as text puts
  "100 Gb/sec" BEFORE "40 Gb/sec", so a descending sort would bury the
  degraded link at the bottom: the sort would defeat the exact purpose the
  column exists for. It sorts on a parsed Gb/s value while still displaying the
  string, and rates phrased in a form the parser does not recognise sort last
  rather than as zero — unknown is not slow.

  **They belong together, and neither works alone.** Sorting without a page
  limit still renders every row, so the section keeps growing as nodes are
  added. Paginating without sorting just hides rows behind a control, and the
  one you wanted is as likely to be on page 9 as page 1. Together, the sort
  decides what "interesting" means and the limit makes the section cost a fixed
  amount of screen at any cluster size.

  Decisions worth keeping if this is rolled out:
  - **Sort cycles desc → asc → the table's own order.** Descending first
    because every numeric column here has "most" as the interesting end.
    Returning to the table's deliberate order matters — that order encodes
    reasoning (models lead with what is serving) and a control that could not
    get back to it would throw that away.
  - **Nulls sort last in both directions.** A missing reading is not a small
    one, and letting nulls lead an ascending sort fills page 1 with rows that
    have nothing to say.
  - **The pager reports a RANGE, not a page number.** "11–20 of 288" answers
    "how much am I not looking at"; "page 2 of 29" makes you do arithmetic.
  - **The pager only renders when it does something.** Under a six-row table it
    would be chrome claiming there is more.
  - **The page index is clamped on read**, because the row count moves: a node
    going away can shrink the table while you are on its last page, and being
    stranded on an empty one reads as broken data.

  **The row cap is a setting, because the page size was never one number.**
  "10 is a guess" was the open question; the answer is that it depends on the
  section and on the monitor. Settings → Sections carries a per-section cap
  (5 / 8 / 10 / 15 / 25 / 50 / all), defaulting to 10 — except Network, which
  defaults to 8 because it draws TWO tables and the cap applies to each, so 8
  there is 16 rows of section against 10 for a table that draws one.

  **It was requested as a layout fix and it is one.** With two columns that
  fill independently, a column's height is the sum of its sections, so one
  unbounded table sets how far you scroll. Capping puts a ceiling on that: at
  four nodes it took GPU processes from 661px to 337px and Network from 1104px
  to 556px, which is what makes two sections of similar size actually sit
  level beside each other.

  **`all` is a real option and it broke the arithmetic.** Uncapped means
  `pageSize = Infinity`, and the first row index is then `0 * Infinity` — NaN —
  which makes `slice(NaN, NaN)` return NOTHING. "Show me every row" rendering
  an empty table is the worst available failure and it is one multiplication
  away, so `#start` guards finiteness in one place rather than at each call.

  Model activity is a chronological timeline with no columns, so it has nothing
  to sort — its `TableView` is constructed with an empty column list and used
  only for paging, which keeps the page clamping and the range wording
  identical to the tables. Its sort equivalent is the time window it already
  has.

  Still open: whether sort state should survive a reload. Currently it does
  not, and the argument for leaving it that way is that a sort is a question
  you asked once, where a row cap is a preference.

- [ ] **M1b. Original sorting note.** Clearly worth it. "Which model is actually serving" is a
  sort by tok/s, and today it is a scroll. Client-side, since the data is
  already in the page.

  **Must keep a DEFAULT sort that is the current order.** Each table's order is
  deliberate — models group by node then router, processes lead with the
  biggest consumer — and a sort control that cannot return to it would destroy
  reasoning the tables were built around.

- [ ] **M2. Filtering, but as one interaction rather than a widget per
  section.** The high-value filter is by node, and the page already has a
  natural control for it: the node cards. Clicking one to scope every table to
  that node beats four separate filter boxes, and it is the interaction people
  already expect from a card that represents a thing.

  The other filter that pays immediately is hiding `unloaded` models — 32 of 36
  rows today. Probably a toggle rather than a general predicate.

- [ ] **M3. More columns, chosen — NOT a column picker.** This is where I would
  push back on the original suggestion.

  Configurable columns are the move you make when you cannot decide what
  matters. Every column in these tables earned its place and most carry a
  comment explaining why; handing that decision to each user turns a curated
  view into an assembly kit, adds persistent state to maintain, and means no
  two people see the same dashboard when comparing notes.

  The legitimate kernel is real though: there IS room now, and data the agent
  already collects is not shown. The answer is to decide what else is worth a
  column and show it to everyone — per-process SM% is already collected, model
  size and last-used would help ranking, per-node throughput exists. Pick, do
  not delegate.

  Revisit only if two users genuinely need different columns for different
  jobs, which is not the case for one operator and a homelab.

- [x] **M4. Column visibility, controlled from the card — shipped 2026-08-17.**
  Designed and built the same day; a control on each card, not a page in
  settings. `lib/columns.svelte.ts` (`ColumnView`, `columnStore`) and
  `components/ColumnMenu.svelte`.

  **Columns, not rows — and the distinction is the first design decision.**
  What this does is show and hide the COLUMNS of a table. Filtering the ROWS
  ("only gx10-b") is M2, and it hangs off clicking a node card. Both will
  exist, which is why the funnel glyph is NOT used here: a funnel conventionally
  means row filtering, and spending it on columns would leave M2 without its
  obvious icon. This takes a columns/table glyph; the funnel stays reserved.

  **It reopens M3, and M3's objection was right.** M3 argued against a column
  picker: every column earned its place, most carry a comment saying why, and
  delegating that turns a curated view into an assembly kit. That still holds
  for what the DEFAULT shows. What changed is that M3's own escape clause —
  "revisit only if two users genuinely need different columns" — has been hit
  twice. So M4 is an override on a curated default, never a build-your-own
  table: the shipped column set stays the considered one, and hiding is opt-in
  per browser.

  **Three of the five cards get it.** GPU processes, Models, Network. History
  already HAS this feature — its metric chips are column selection for a chart,
  and a second mechanism for one idea would be worse than none. Model activity
  is a timeline rather than a table, with one hideable field (`node · router`),
  which probably does not justify a control.

  Settled:

  - **A columns glyph in the card header's far right, hover-revealed** like the
    drag handle — but STAYING visible whenever something is hidden. Every card's
    top-right already carries a count or a button group, so the control sits
    after them, and the page keeps reading as an instrument panel rather than a
    toolbar. The persistent-when-active rule is not decoration: a missing column
    with no visible cause reads as the backend having broken.
  - **Network gets ONE menu with two labelled groups** (RDMA ports,
    Interfaces), because it is one card with two tables. One button per card is
    the rule.
  - **A hidden alert-bearing column unhides itself when it goes non-zero**, and
    says why. `err` and `drop` read zero every day, which is exactly why someone
    switches them off, and their first non-zero value is the thing they needed
    to know. This is a monitoring dashboard: hiding a stat is hiding a signal,
    and the resolution is that the signal wins. Consistent with the page already
    receding when data goes stale.

  **The hazard that decides the implementation.** Every `<td>` is hand-written
  in fixed order. Hiding a column means the header list and the cell list must
  agree, and if they ever disagree EVERY VALUE SHIFTS INTO THE WRONG COLUMN —
  which looks like corrupted data rather than a broken UI, and is therefore
  worse than a crash. So the rows become data-driven: one `{#snippet}` per
  column, keyed by the same `ColumnDef` that drives the header, so order and
  visibility have exactly one source. That refactor across four tables and ~32
  columns is the bulk of the work, and it is the reason this is not a small
  change. It is the same failure `ColumnDef` already exists to prevent for sort
  keys, one step further on.

  Rules to build in from the start, each already learned elsewhere here:

  - **Identity columns cannot be hidden** — `ColumnDef` gains `required`. A
    table of numbers with no `node` column is unreadable, and it is precisely
    the mistake a picker invites.
  - **Hiding the sorted column drops the sort** back to the table's own order.
    An invisible sort on a visible table reads as the data being wrong — the
    same class of bug as a header that sorts by its neighbour.
  - **`reset` restores everything**, the same unrecoverability lesson as hidden
    sections: a thing hidden from the page it is hidden from has no way back.
  - **Per browser** (`spark-dash.section-columns.v1`), like every other view
    preference. The backend is deliberately stateless, and a column set tuned
    for a 34" monitor is not the one you want on a phone.
  - **No automatic hiding at narrow widths.** Same reasoning as K4's
    Full/Compact: a page that rearranges itself unprompted is disorienting, and
    the person who wants fewer columns can say so once.

  **What shipped, against the plan.** All of it, and the design held up. Two
  notes from building it:

  - **The checkbox reflects the CHOICE, not the state.** A forced-back `drop`
    column shows an unchecked box with "shown — not zero" beside it, rather than
    silently re-checking itself. A switch that flips itself is a switch you
    cannot trust, and the reader needs to be able to see both what they asked
    for and what is overriding it.
  - **Dismissal tests need a real element as the event target.** Clicking
    "outside" by dispatching on `window` left the menu open and looked like a
    bug in the handler; with a real element it closed correctly. The handler is
    now an explicit `instanceof Node` check rather than a cast, because
    `contains()` given a non-Node is not reliably falsy and the failure mode is
    a menu that will not close.

  Verified: hiding two columns leaves every row with exactly as many cells as
  there are headers, and the values stay under the right ones — `node` still
  reads `gx10-a` and `sm` still reads `0%` after two removals. Hiding the sorted
  column drops the sort. `err` (all zeros) hides; `drop` (36 on `enP7s7`) refuses
  to stay hidden. Reset restores all eight columns and clears the key. Column
  widths stay constant afterwards, so N's reservations are intact — the one-time
  change on a click is the only movement.

**Ordering:** M1 is done — sorting, pagination and the row cap. M2 next, and
worth designing as card-click rather than filter boxes. M4 after M2 — it is the
same "show me less" impulse, and M2's node filter may satisfy enough of it to
change what M4 needs to be. M3 last, as a deliberate editorial pass rather than a feature, and
it should be settled BEFORE M4 ships: deciding what everyone sees is a
different question from letting one person hide part of it, and doing them in
the wrong order means curating a column set around what people have already
switched off.

### K — Compact node cards, and a grid

**The problem, measured on a real 3-node render 2026-08-17.** Each node card is
**147px** tall in a 907px viewport. The cards are a vertical stack, so:

| nodes | card block | vs one viewport |
|---|---|---|
| 3 | 441px | half the screen before anything else |
| 8 | 1176px | everything else below the fold |
| 16 | 2352px | 2.6 screens of cards |
| 32 | 4704px | 5 screens |

Users in the wild run clusters of 32. At that size the current layout is not
merely untidy, it stops being a dashboard — you cannot see the fleet and a
chart at the same time, which is the whole point of the page.

The full card is right for one to three nodes and it should stay the default at
that size. This is about what happens past that.

- [x] **K1.** Shipped 2026-08-17. Compact card keeps node name, status and the
  **shared memory band** — the one reading that is GB10-specific and cannot be
  inferred from anything else on the page, since models, other GPU work and
  system all draw from one pool. Clock, temp, power, CPU, mem %, throughput and
  pressure drop, along with the runtime summary (a sentence per card is what
  makes a grid of them unscannable).

  Measured on the 3-node test: **147px → 96px**, a 35% saving, which puts the
  History panel above the fold at three nodes. At eight: 1176px → 768px.

  Padding and gap tighten; the type scale does not. Shrinking the text would
  make a compact card harder to read at exactly the moment there are more of
  them to read.

  **A down node is never compacted.** Compact exists to fit more healthy nodes
  on screen; shrinking the one that needs attention would invert the point, so
  it keeps its full treatment and its error text.
- [x] **K2. Hover reveals the rest — shipped 2026-08-17.** Copied from the
  chart tooltip as planned: absolutely positioned, so it costs no layout and
  cannot reflow the grid. Verified by measuring — card 117px and grid 221px are
  identical with the reveal open and closed. A panel that grew on hover would
  shove every card below it down as the pointer crossed the page.

  **Compact hides pixels, not information.** `display: none` and
  `visibility: hidden` both drop content out of the ACCESSIBILITY tree, which
  would have made the compact card withhold these readings from a screen reader
  entirely — turning a density preference into an information one. The reveal is
  clipped instead, so the paint is hidden and the content stays announced.

  Focus reveals it too, on `:focus` rather than `:focus-visible`: if focus is on
  the card the readings are wanted however focus arrived, and `:focus-visible`
  deliberately does not match programmatic or mouse focus. The outline keeps
  `:focus-visible`, which is exactly the case that selector exists for. The card
  is focusable ONLY when compact — in full mode nothing is hidden, so it stays
  out of the tab order rather than adding a stop per node for nothing.
- [x] **K3.** Shipped 2026-08-17. Compact mode flows cards through a shared
  grid (`auto-fill, minmax(300px, 1fr)`), so they stop spanning edge to edge.

  Measured on the 3-node test: the node block went **464px → 117px**, three
  cards on one row. Vertical saving is ~4x, not the 35% K1 alone gave — the
  height came from stacking, not from card size.

  **The structural catch.** Every standalone node is its own "cluster of one"
  and so has its own `.nodes` wrapper. Gridding those wrappers does nothing:
  each contains exactly one card, which is why compact cards still spanned the
  full width. The wrappers now use `display: contents` in compact mode so their
  cards become items of one shared grid.

  Scoped with a CHILD combinator (`> .nodes`). Without it the rule also caught
  the wrappers inside a framed cluster, promoting those cards into the
  cluster's own single-column grid and leaving them full width and stacked —
  which is what the first attempt did.

  A framed cluster spans the full row (`grid-column: 1 / -1`) and grids its
  members inside the frame. The frame means "these pool memory", and one
  covering part of a row would say something untrue about which nodes are
  grouped.

  **Column counts snap to powers of two — 1, 2, 4, never 3.** Clusters scale in
  powers of two, so a 3-wide grid is precisely the one that wastes a row: four
  nodes become 3 + 1 and the second row is nearly empty. Fixed counts rather
  than `auto-fill`, because "as many as fit" is 3 at this container width.

  The shell caps at 1180px, so the grid never exceeds 1140 and four columns
  land at 276px each. The memory band's legend wraps to two lines at that
  width, which costs ~37px of card height — still a clear win, since four nodes
  go from two rows (~204px) to one (133px).

  Verified with four nodes: one row, 276px cards, and only 1/2/4 rules exist in
  the stylesheet.
- [x] **K4.** Settled and shipped 2026-08-17: an explicit Full/Compact toggle
  in settings, persisted to `spark-dash.compact-cards.v1`, defaulting to Full.

  **No automatic switching at all**, including the "compact above a threshold
  on first run" idea floated when this was filed. A page that rearranges itself
  when a node joins is disorienting, and a node joining is exactly when someone
  is watching it. The person who needs compact turns it on once and it stays
  on. `reset` clears it along with the rest of the layout.

- [x] **K5.** Pooled memory band for a framed cluster, shipped 2026-08-17. The
  cluster's shared pool was a line of text while every node beside it had a
  bar; it is the same quantity and deserved the same treatment.

  Reuses `MemoryBand` unchanged, summing members' total, used and processes, so
  the pooled bar splits by the same workload classes — models, other GPU work,
  system — that a single node's does.

  **Only a framed cluster gets one.** That bar is honest here precisely because
  these nodes are clustered: a model can span them, so their combined free
  space is a number an operator can act on. The identical bar drawn across
  UNCLUSTERED nodes would describe capacity that does not exist, which is the
  error the `cluster` field exists to prevent.

  Down members contribute nothing — neither capacity nor consumption. Counting
  a dead node's last-known processes would describe memory nobody is holding.

**Still open: K2** (hover reveals the rest). Until it lands, the only route
back to clock, temperature and power is toggling to Full.

**Accent colours — fixed 2026-08-17, and it was already broken.** Node
identity used `--series-${slot % 3 + 1}`, which cycles at the FOURTH node: a
four-node cluster gave two nodes the same hue, which is colour that has stopped
identifying anything. Found the moment 4-wide rows were tested.

Now `--chart-1..8` — the same palette extended, whose first three ARE the old
node hues (so one to three nodes are unchanged), validated as a categorical set
for CVD separation against every theme's surface. **Past eight, no colour at
all**: the card takes a neutral rule and identity rides on the node name, which
is on every card anyway. Generating a ninth hue or wrapping around would both
reintroduce the collision.

The slot itself was also wrong. It came from the cluster index plus the member
index, so a two-member cluster at index 1 took slots 1 and 2 and the next
cluster — index 2 — took slot 2 as well. Now a flat running count across the
whole page, which cannot collide.

**Related:** the same pressure applies to the tables below (processes, models,
network) — 32 nodes multiplies every row count. K only fixes the cards; the
tables are a separate question, probably filtering rather than compaction.

### N — Arranging the sections — **shipped 2026-08-17**

Two columns for the sections themselves, so a tall table and a short one can
sit side by side. Went through three implementations; the first two were wrong
in ways worth recording, because both looked reasonable on paper.

**Attempt 1 — a two-column CSS grid, half/full per section.** Correct-looking
and fundamentally unable to do the job. **A grid packs by ROWS, and a row is as
tall as its tallest item.** Put a short section beside a tall one and the space
under the short one belongs to that row and nothing can occupy it. Measured on
the running dashboard: `models` (337px) beside `processes` (661px) stranded
324px — and `activity` (167px) would have fitted in it twice. No amount of
`dense` packing or auto-flow tuning fixes this; it is what a grid IS.

**Attempt 2 — live reordering during the drag.** Sections swapped as the
pointer crossed them. Every problem it had traces to one property: **the layout
was rearranging underneath the thing being aimed at.** Each swap moved the
dragged card's home, so the lift had to be re-anchored to stop it jumping; the
compensation moved the card; the moved card changed what was under the pointer;
that re-triggered the swap. Direction gating, one-step-at-a-time, FLIP
animation and a settle-gate were all added to damp a loop that should not have
existed.

**Shipped — three zones, and the drag only aims.**

*Zones.* A full-width band above two columns that fill INDEPENDENTLY. They have
to be separate elements: independent fill means there is no row for their
contents to align to, which is precisely what a grid cannot express. A section
is `full`, `left` or `right`, stored in `spark-dash.section-placement.v1`.

*The band is above the columns, and that is a real constraint.* A full-width
section cannot sit BETWEEN column content, because two independently-filling
columns share no horizontal line for it to interrupt. Accepted deliberately:
the wide thing is the history chart, which belonged at the top anyway.

*Aim, then drop.* Dragging shows a line where the section WILL land and moves
nothing until release. This is the fix for attempt 2 and it is a deletion, not
an addition — no compensation, no FLIP, no settle-gate, no way for a reorder to
feed back into the targeting that caused it. Escape cancels, which is free to
offer once a drag has nothing to undo.

*Ordering stays one array.* Zone membership filters `order`, and filtering
preserves relative order — so each zone's sequence is independent for free,
while there is still a single list to reconcile against added or removed
sections.

Details that were bugs first:

- **An empty zone is 0px tall**, so nothing could be dragged into one — and the
  first move anyone makes is out of the default single stack, into an empty
  column. Empty zones become labelled dashed targets for the duration of a
  drag, and are invisible otherwise.
- **The drop line draws ABOVE the carried card.** A section is exactly as wide
  as the column it is aiming at, so whenever the pointer nears the destination
  the card covers it — hiding the one thing the drag exists to show.
- **`nearest zone` rather than `zone under the pointer`.** The zones do not tile
  the window: there are gaps between columns, margins either side, and
  everything below the last card. Without the fallback the target blinks out
  whenever the pointer strays into any of that, which reads as a broken drag.
- **`reset` removes the old width key**, not just the new placement one.
  Placement falls back to migrating half/full when absent, so a leftover width
  key resurrects the arrangement that was just reset.

Half/full arrangements migrate on first load — full stays full, halves
alternate into the two columns in their existing order. Keyboard: up/down
within a column, left/right between zones.

**Layout shift — fixed 2026-08-17, and the cause was `1fr`.**

Measured before: **CLS 0.1488 across 91 entries**. Every shift was HORIZONTAL,
which is the clue — a dashboard that jitters sideways is not being pushed by
content arriving, it is being resized by it.

`1fr` is shorthand for `minmax(auto, 1fr)`, and that `auto` minimum means a
track refuses to be narrower than its content's minimum. These tracks hold wide
data tables, so the minimum is both large and VARIABLE. Measured live, the two
"equal halves" were **813.273px and 769.727px** — the left had taken 43px from
the right purely by containing wider tables. Every time a live value gained a
digit ("2.9 GiB models" becoming "107.5 GiB models"), the content minimum
changed, the track resized, and both columns moved.

Capping the track minimum at 0 — `minmax(0, 1fr)` — makes the tracks exactly
equal and immovable; content that genuinely does not fit scrolls inside its own
`.scroll` box, which is what that box is for. Applied to `.cols`, `.zone`,
`.sections`, both node grids and the timeline's row template.

Two things worth knowing for next time:

- **The propagation is blocked at EITHER end and both were applied.** Tested
  directly: with `.cols` on a bare `1fr` and `.zone` on its implicit `auto`
  track, a 1400px-wide table blows both tracks out to 1402px and the whole page
  scrolls sideways. Fixing either one alone holds it at 791.5px. Both are set
  because a section nested differently in future would reintroduce the path.
- **`overflow-x: auto` on the inner `.scroll` box did NOT stop it.** That is the
  intuition to discard — the table's width still reached the grid tracks, so
  the fix has to be on the track, not on the scroller.

After: **CLS 0**, confirmed from `performance.getEntriesByType('layout-shift')`
being empty rather than only from an observer.

A grid with no explicit `grid-template-columns` is the same bug wearing a
disguise: the implicit track is `auto`, which sizes to content. `.sections` and
`.zone` now state `minmax(0, 1fr)` rather than leaving it implicit.

**Part two: the same bug one level down, inside the tables.** Fixing the grid
left the Network card still shifting, because auto TABLE layout sizes columns
to content for exactly the same reason. Measured over 45s of live data:

    rx   66 → 81px   (Δ14)      table width 813 → 825 in an 813px box
    tx   59 → 73px   (Δ14)      → horizontal scrollbar appearing and vanishing

`bits()` returns anything from "0 b/s" to "200.00 Gb/s", so throughput is the
one genuinely volatile width on the page. Two columns swinging 14px dragged
every OTHER column 2-9px with them as auto layout redistributed the difference,
and pushed the table across its container so the scrollbar flickered.

The fix is to reserve, per column, the widest string that column can produce —
`min-width` in `ch`, which is exact because these cells are monospace with
tabular figures. Sized to the HARDWARE, not the formatter: 11ch covers
"200.00 Gb/s" and every character reserved is one taken from the interface name
beside it.

**`ch` alone was wrong and the first attempt shipped nothing.** These cells are
`border-box`, so `min-width: 11ch` reserves eleven characters INCLUDING the
5px/12px padding — about 7.7ch of actual room, narrower than the content it was
meant to cover, so the columns went on resizing. It has to be
`calc(11ch + 24px)`. Verify a reservation is doing anything by checking that the
column's rendered width EQUALS its min-width; if the column is wider, content
is still in charge.

**Text columns need a ceiling, not just a floor.** A reservation fixes a column
whose VALUE changes; it does nothing for one whose set of ROWS changes. GPU
processes kept moving 27px after the numbers were pinned, because a transcode
starting or a model unloading changes which strings are present, and the column
is sized by the widest one currently on the page. `runtime` and `model` take a
floor and a ceiling with ellipsis, full name on the title.

**Getting the RDMA table to fit, rather than to scroll stably.** It was 837px of
content in an 813px column — nine columns, several of them identifiers. Three
changes closed the gap without touching an identifier:

- `negotiated` → `rate` as a header. The longer word was setting the column's
  width by itself: the header, not the content, was the binding constraint, so
  abbreviating the value alone saved nothing. Worth checking which of the two
  actually binds before optimising either.
- The value drops its trailing "(4X EDR)" and shortens "/sec" to "/s" — the
  latter also settling an inconsistency inside this panel, where rx and tx
  already read "kb/s" from `bits()`. The driver's unabridged string is on the
  title. 97px → 81px, with no ellipsis needed because the string is now short
  rather than clipped.
- `err`/`drop` reserve 5 digits instead of 6, the last 7px. Cheap because this
  is a number read as "is it zero", not digit by digit; past 99,999 the column
  grows once and stays, which is a one-way trip rather than the oscillation the
  reservation exists to prevent.

Result: all four tables fit their column exactly, every column delta 0 over 55s
of live data, no scrollbars, CLS 0.0003 — and that remainder is sub-pixel, with
the reported sources showing dx=0 dy=0.

### O — History as small multiples — **shipped 2026-08-17**

Designed and built the same day. `MetricChart.svelte` replaces `CombinedChart`;
`TrendChart` and `combine()` are deleted. Net −365 lines.

**The problem.** The History panel was built around one
node and does not survive a cluster. Measured on the four-node test rig, with
seven metrics selected:

- **28 lines in 7 colours.** Series are coloured by METRIC
  (`metricColor(s.slot)`), so all four nodes' GPU temperature are the same
  orange. Which line belongs to which node is only answerable by hovering.
- **The node legend colours nothing.** Checked directly: no series stroke
  matches any legend swatch. The legend maps colours to nodes and no line on
  the chart uses them — it is decoration that looks like a key.
- **The legend collides at the fourth node anyway.** `nodeColor()` is
  `--series-{slot % 3 + 1}`, so `gx10-a` and `gx10-d` are both blue. The node
  CARDS were moved to `--chart-1..8` when this same bug was fixed there; the
  two agree for three nodes and diverge from the fourth.
- **`TrendChart.svelte` is dead code.** Nothing imports it.

**One chart per METRIC, not per node.** Per-node was the instinct and it is the
wrong axis, for a reason that has nothing to do with taste: the chart count
would grow with the cluster, which is the exact problem being solved. Per-metric
is capped at the metric list — eight charts at 32 nodes as at 1.

Two things follow from it that are worth more than the layout change:

- **One unit per chart means a real axis.** The present normalisation — every
  series divided by a fixed ceiling onto a shared 0-100% scale — exists ONLY
  because metrics of different units share one plot. Split them and it goes,
  along with the "scaled to the window's own maximum" caveat for every metric
  that has a ceiling.
- **Colour becomes the node**, which is what the legend already claims and what
  the cards already use. The `nodeColor` / `--chart-1..8` divergence gets
  resolved by making the charts use what the cards use.

**The node legend becomes the control.** This is what completes the design
rather than decorating it: toggling nodes across every chart at once takes the
panel from "which node is different?" to "what is this one box doing?" without a
second layout. It needs no new real estate, because the legend is already there
with one swatch per node — it just does nothing today.

Settled:

- **Click solos, click again restores.** One click each way for the drill-down
  case; shift-click adds a second node for a pairwise comparison. Plain per-node
  toggles would be seven clicks to isolate one of eight.
- **Not persisted.** Scoping to one node is a question you ask, not a preference
  you hold. On a monitoring dashboard a node you forgot you deselected is a node
  whose history you have stopped watching — the same hazard as a hidden `err`
  column, resolved the same way, by refusing to let the state outlive the
  session. Cards and alerts are unaffected either way, so the blast radius is
  small, but resetting removes it entirely.
- **Fixed ceiling per metric, real units.** 0-100°C, 0-300W, 0-100%. Heights
  stay comparable between nodes and across time, a quiet chart genuinely looks
  quiet, and — the reason this interacts with the toggles — switching a node off
  does not rescale every chart under the cursor. Throughput has no natural
  ceiling, still auto-fits, still flagged.
- **The metric chips keep choosing which charts are drawn.** They now control
  the section's height as well as its content, which matters more than it used
  to now that sections sit in columns under a row cap.
- **Never zero nodes and never zero metrics**, the rule the chips already have:
  an empty plot reads as broken rather than as a choice.

Layout: up to 4 per row, snapping 1 / 2 / 4 — powers of two, the same reasoning
as the node grid in K, because a 3-wide row is the one that strands a row on a
power-of-two fleet.

Watch for, when building:

- **Colour must follow the node, not its position.** Deselecting `gx10-b` must
  not repaint `gx10-c`. `nodeSlots()` already derives slots from the full
  ordered list rather than the visible subset, which is exactly right — it is
  also exactly the kind of thing that quietly breaks when the visible list
  becomes the input.
- **`combine()` does not simply get deleted.** Its merging of several metrics
  into one series list does go. But its timestamp SNAPPING exists because
  parallel range queries come back on grids offset by milliseconds, and a
  synchronised cursor across charts still needs those grids to line up or the
  crosshair will land on different instants in each. Keep the snapping, drop the
  merge.
- **Synchronised cursor is not optional for small multiples.** Reading eight
  charts at one instant is the entire point; uPlot has `cursor.sync` for it.
- Eight uPlot instances instead of one — worth measuring before assuming it is
  free, particularly on a 7d range.

**What building it turned up.**

- **An unknown node used to take slot 0's colour.** Callers reach the palette as
  `nodeColor(slots.get(name))`, and the old `?? 0` fallback silently painted an
  unrecognised node in the FIRST node's hue. A history series can legitimately
  name a node the current inventory does not — one removed from `cluster.yml`
  or renamed still has samples for the rest of the window. `nodeColor` now takes
  `number | undefined` and gives an unknown node the neutral, which is the same
  rule as past-eight: no hue beats someone else's hue.
- **Cursor sync makes every chart render its own tooltip.** Sync fires
  `setCursor` on all of them, which is what moves the crosshairs together — and
  meant one hover produced six overlapping boxes. The crosshair belongs on every
  chart; the numbers belong on the one being pointed at, gated on a local
  pointerenter.
- **A fixed 44px y-axis clipped its own numbers**, rendering "3003MHz" as
  "20MHz". An axis that silently truncates is worse than no axis; the width is
  now derived from the widest label the metric can print.

**Fixed straight after shipping: clicking a node blanked every chart.** The
legend was built from `nodeIds` — the LIVE inventory — while the lines come from
Prometheus, and those two sets are not the same thing. Selecting a node with no
history filtered every series out, leaving each chart a caption with no plot,
which reads as the panel having broken rather than as an empty selection.

Found in the test rig, where the fake agents are live-polled but never scraped,
so the legend offered four nodes and history knew only `sparky`. It is not a rig
artefact though: a node just added to `cluster.yml`, or scraped for less than the
window, does exactly the same thing in production.

Three parts to the fix, and the first is the rule:

- **The legend is the key to the LINES, so it lists the lines** — the nodes
  actually present in the loaded history, in inventory order so colours stay
  stable, plus any history knows that the inventory does not. A node recently
  removed from the cluster still has samples for the rest of the window and IS
  drawn, so it belongs in the key.
- **A live node with no history is LISTED AND DISABLED**, not dropped. The
  first fix hid it, which stopped the blanking and also removed the control:
  with one node plottable the legend fell below its "more than one" threshold
  and the node toggles vanished entirely, reading as the feature having been
  taken away. Reported as a regression, and fairly.

  The legend now lists the inventory plus anything history knows, with
  unplottable nodes disabled and the reason on the title. "Off" and "cannot be
  turned on" stay distinguishable on the swatch: a deselected node keeps its
  identity colour, an unplottable one goes neutral.

  Not struck through, which was the first attempt at that distinction — a
  struck-out node name on a monitoring dashboard reads as dead or removed,
  the opposite of what it means. The node is up; it just has no history yet,
  usually because it was only added a moment ago.
- **A selection that no longer matches anything collapses back to "all".** A
  refresh can retire the very node that was soloed; holding a dead selection
  would blank the panel with no obvious way back.

**Also fixed: a metric with no samples vanished, so its chip did nothing.**
`drawable` filtered on `data[m.key]?.x.length`, which dropped any metric that
came back empty — on an idle cluster that is Throughput, and toggling its chip
changed nothing on the page. A control that does nothing is broken.

It also threw away a reading. "No throughput in this window" MEANS nothing was
serving, which is exactly the kind of thing this panel exists to tell you; an
absent chart cannot say it and an empty one can. A selected metric now always
gets a chart once loaded — still keyed on the entry EXISTING rather than on its
length, so a metric mid-fetch is held back instead of flashing an empty frame
before its data lands.

**An EMPTY CHART, not a message where a chart should be.** The first attempt put
"No samples in this range" in a dashed box, which is a different kind of object
in a grid of plots. An empty chart is the consistent answer and the more useful
one: the axes are doing real work with no line on them, because they say what
the scale is — an empty 0-300W plot reads as "nothing drew power" rather than as
a panel that failed. uPlot needs at least one y series to lay a plot out, so an
empty metric gets one made of nulls, and it borrows its time axis from a metric
that did return data so the grid's axes all agree.

MetricChart's ResizeObserver moved from `onMount` to an effect keyed on the host
element while this was in flux: the plot element came and went, and an observer
attached once at mount would end up watching a detached node and leave the chart
at its guessed initial width.

Verified with the four-node rig: all four legend colours are drawn on the
canvas (sampled pixels, not assumed); solo, shift-add and restore each change
which lines are drawn; a soloed node keeps its own colour rather than
repainting to slot 0; and a hover on any one chart puts all six tooltips on the
same instant — checked at three cursor positions and from two different charts.

The test rig's Prometheus only knows the real node, so the multi-node paths were
exercised by stubbing the history response in the browser. That is a rendering
test and nothing more: it says the chart draws four nodes correctly, not that
the backend returns four.

**Axis text was unreadable, and had been all along.** `chartTheme()` returned
`axis: cssVar('--rule')` — and uPlot uses an axis's `stroke` for its TICK
LABELS, so every number on every axis was painted in the hairline/border token.
Measured against the panel: **1.24:1 in dark, 1.29:1 in light, 1.56:1 in
cyberpunk**, against a 4.5:1 floor for text that size. Barely above invisible,
and it predates the split into small multiples — the old combined chart had it
too, on eight times fewer axis labels.

The fix is to stop using one token for two jobs. Grid and tick LINES keep
`--rule` and stay recessive, which is correct: a grid is a reference, not
content. The label text takes `--ink-muted`, which clears the floor in all three
themes while staying subordinate to the plotted lines — and is what the table
column headings already use, so an axis and a table header now read at the same
weight, which is what they are.

Verified from the rendered CANVAS PIXELS rather than from the token, since uPlot
resolves colours at build time: **6.08 / 4.66 / 6.25** across dark, light and
cyberpunk, with gridlines still sampling at 1.56.

Axis font also went 10px → 11px. These are eight small charts rather than one
large one, so their axes carry the densest type on the page and were also its
smallest.

**Y-axis labels were being clipped, and it took three attempts to size that
gutter.** Worth recording all three, because the first two look right:

1. A flat `44px`. Turned "3003MHz" into "20MHz".
2. Sized from the CEILING's formatted label. Still clipped "50.0°C" — because
   **the widest label is usually not the ceiling**. `fmt` added a decimal below
   100, so the MIDDLE split "50.0°C" (6 chars) beat the top one "100°C" (5).
   A percentage axis was surviving on 1.5px of margin.
3. Passing uPlot a `size` CALLBACK, which hands you the formatted values. This
   looked like the clean answer and was the worst of the three: it is called
   before those values exist, so it returned bare padding and clipped **every**
   chart on the page.

What works is measuring the labels this axis can actually print — candidates
across its own scale, in the font that will draw them, via an offscreen 2d
context. No callback, no characters-times-a-constant estimate. The auto-fitted
case takes its top from the tallest sample rather than a ceiling it does not
have, so a throughput axis reaching 1500 gets room for "1500tok/s".

The decimals went too, and that was the same bug wearing a different hat.
"50.0%" carries a tenth of a percent the axis cannot resolve — it says nothing,
and it was what made the middle label the widest. Ticks are integers unless the
scale is genuinely small, which keeps throughput's 0.0 / 0.5 / 1.0 readable.

A trap worth recording for anyone testing theming here: setting `data-theme` on
the documentElement directly does NOT restyle a chart. Canvas colours are
resolved when uPlot builds, and the rebuild is keyed on the `themeKey` prop, so
a theme switched underneath the app leaves every chart painted in the previous
theme's colours. Switch themes through the app's own control, or the measurement
is of the wrong thing — it read 2.98:1 for light and the pixel turned out to be
dark's muted ink.

**Deferred: past eight nodes** there are not enough distinguishable colours for
one line each, and the answer is probably a min/median/max band with the
outliers named rather than 32 lines. Additive, and hypothetical at 1-4 nodes, so
it is not being decided now.

**Related:** M2's page-wide node filter will eventually want to converge with
these toggles. Deliberately kept History-local for now — one panel's control
that later merges is a smaller mistake than a page-wide filter built early that
M2 then has to fight.

### J — Single-host profile (everything on one GB10)

**The premise this project was built on:** the GB10 is an inference workhorse,
and monitoring should cost it as little as possible. Most other Spark
dashboards run directly on the Spark; this one deliberately does not, and the
node stack is kept to two containers with no persistent state precisely so the
box stays free for models. That is a design position, not an accident, and J
must not quietly erode it.

**But not everyone has a spare machine.** A user with one GB10 and no Proxmox
host currently cannot run this at all, which is a worse outcome than a
documented, opt-in single-host mode whose cost is stated up front.

**Already possible — verified 2026-08-16, no code changes needed.** Every image
in the central stack publishes linux/arm64 (`prom/prometheus`,
`prom/alertmanager`, `prom/node-exporter`), and `publish-images.sh backend`
run ON a GB10 produces an arm64 image natively: built in 7s, 186MB, `/health`
200, frontend served. "Compiling for arm64" is not the work.

**Measured footprint, so the trade is a number rather than a claim:**

| | memory | notes |
|---|---|---|
| node stack today | **~101 MiB** | agent 91 + node-exporter 10 |
| central stack adds | **~159 MiB** | backend 79 + prometheus 50 + alertmanager 21 + exporter 10 |
| TSDB on disk | **79 MB** | at 180d retention, one node, ~4.3k series |

On a 121 GiB unified pool that is ~0.13% of memory — negligible against a
model, but it is not zero, and on GB10 it comes out of the *same* pool the
models use. See [[gb10-unified-memory-constraint]].

- [ ] **J1.** `central/compose.single-host.yaml` (or a compose profile) that
  drops `sparkdash-central-node-exporter`. On one box it duplicates the node
  stack's exporter on `:9100`, producing near-identical series for the same
  host; point the `node-exporter-central` job at the node stack's instead.
- [ ] **J2.** A `cluster.yml` example for self-addressing. The backend polls
  each node's `host` from inside a container, so `localhost` does not work —
  it needs the host's LAN IP. Currently an undocumented gotcha that would stop
  a first-time single-host user cold.
- [ ] **J3.** README section stating the trade honestly: the failure domain
  collapses. The whole reason central lives elsewhere is that "node down" is a
  primary alert, so hosting Prometheus on the node means a crash destroys both
  the node and the history explaining why. Single-host users accept that; they
  should not discover it during an outage.
- [ ] **J4.** Surface the cost in the dashboard itself. The GPU process table
  already attributes memory per process, so a single-host install can show what
  monitoring costs *on this box* — turning the footprint argument into a live
  number the user can watch. This is the feature that keeps J honest.

**Pairs with H.** "Runs on one box" is the first thing anyone evaluating a
public repo will try, so J1–J3 are effectively part of a credible quickstart
(H4).

### H — Genericize for distribution

Not urgent, and only worth doing if publishing this repo publicly becomes a
real goal rather than a hypothetical. Recorded now because the surface is
small and contained, and it grows quietly if left unnamed.

The repo already ships portable artifacts — both `compose.yaml` files run from
a fresh clone with no edits, and every host-specific value lives in a
gitignored file with a committed `.example` beside it. What remains is a short
list of MY values sitting in tracked files where a placeholder belongs:

- [ ] **H1.** `central/.env.example` and `node/.env.example` — carry
  `forgejo.indielab.tech` as the registry and `192.168.50.x` addresses
  (`BACKEND_URL`, `ALERTMANAGER_EXTERNAL_URL`). Replace with placeholders
  obvious enough that leaving one unedited fails loudly rather than quietly
  pointing at somebody else's LAN.
- [x] **H2.** `scripts/publish-images.sh` no longer hardcodes a registry.
  `REGISTRY` and `OWNER` now default to whatever the clone's own `origin`
  remote points at, so a fork publishes to its own registry with no
  configuration and nothing personal is committed. Env vars still override for
  the case where the registry is not where the source lives.

  Also gained `--no-push` (build with no registry or login at all, which is
  what an evaluator needs), `--tag`, `--no-latest` and `--help`. Documented in
  [deployment.md](deployment.md#building-and-shipping-images).
- [ ] **H3.** Sweep the READMEs for `/docker/spark-dash-homegrown` and
  `192.168.50.x` used as *instruction* rather than *example*. Most are
  illustrative and fine; the distinction is whether a reader would paste it.
- [ ] **H4.** A quickstart that a stranger can follow end to end, which is the
  real test of whether H1–H3 are done — the current READMEs assume the reader
  is me.

**H1 gates publication; the rest is polish.** `forgejo.indielab.tech` is the
*default image* in both compose files, not just documentation — so an
unconfigured clone pulls from a registry the user cannot reach. That has to be
a local-build default before anyone else runs this. The other items can follow.

**Git history is kept as-is. Decided 2026-08-16.** The hostname appears in 9
historical commits, and rewriting them was considered and rejected:

- No secrets are tracked. The ntfy topic URL lives in `secrets/`, gitignored,
  and the README teaches generating your own. A scan for credential-shaped
  strings turns up only the `./secrets` mount path.
- Every address in the repo is RFC1918.
- The hostname is **already public**: `forgejo.indielab.tech` resolves on
  1.1.1.1 and 8.8.8.8 to `192.168.50.103`. Its existence is not a secret the
  repo would leak, and it is unreachable from outside.

A rewrite would also change every sha, invalidating the image tags recorded in
`.env` files and in `sparkdash_agent_build_info`. The history is worth more
than that — much of the reasoning in this project lives in commit messages.

**If published, it goes to GitHub, not Forgejo** — that instance is
LAN-internal permanently. Users would clone, build locally and deploy, with no
registry involved at all: `:latest` there means "what I last built", and
`git pull && build && up -d` is the whole update path. The registry half of
`publish-images.sh` stays a maintainer-only path.

**Also explicitly NOT part of this: build-on-deploy.** Decided 2026-08-16
after confirming the deploy tool supports it. Keeping the build a separate,
explicit step preserves one-line rollback with no rebuild, keeps every node on
bytes known to be identical, and keeps `BUILD_VERSION` truthful — compose
cannot compute a git sha, and a defaulted `unknown` would make `AgentBuildSkew`
unable to fire. The investment went into making the build script flexible and
documented instead. Full reasoning in
[deployment.md](deployment.md#why-not-build-on-deploy).

Revisit only if the deploy tool can pass the deployed commit as a build arg.

**Explicitly NOT part of this:** making the compose files configurable by
path. Their relative mounts are what make a clone self-contained, and that
property is the thing being preserved here, not a limitation to fix. See the
header comment in `central/compose.yaml`.

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
6. **Deployment: settled 2026-08-16 — one repo, started by hand.** Dockhand was
   tried and dropped. Everything now lives in `spark-dash-homegrown`: clone it
   on a host, `cd central` or `cd node`, `docker compose up -d`.

   **What the three repos were for, and why they went away.** `spark-dash-stack-central`
   and `spark-dash-stack-node` existed only because Dockhand deploys *a repo* —
   they held nothing but a copy of `deploy/*` plus a generated `SOURCE.md` and a
   host-local `.env`, kept in step by `sync-stack-repos.sh`. Without Dockhand
   there is nothing for a derived repo to do, so both are archived and the sync
   script is deleted.

   The two-phase orchestration-repo plan sketched on 2026-08-15 is therefore
   moot. Its principle still holds and is what makes one repo safe: *a file
   that is hand-edited on the host must live where nobody pulls over it.* That
   is now enforced by `.gitignore` rather than by repo boundaries — `.env`,
   `cluster/`, `prometheus/`, `alertmanager/`, `secrets/` and `targets/` are all
   ignored, so a `git pull` in a running stack cannot touch them.

   **Layout, flattened 2026-08-16.** `deploy/central` and `deploy/node` became
   `central/` and `node/` at the repo root, and **`DATA_ROOT` was removed**.
   Every bind mount in `central/compose.yaml` is now `./something` relative to
   the stack directory.

   `DATA_ROOT` existed to stop the generated targets directory colliding with
   the hand-maintained one — not because anything needed relocating, and it
   defaulted to the stack directory anyway, so the two mounts resolved to one
   path and the "separation" was fictional. Moving the hand-maintained targets
   into `config/vllm-targets.yml` — filing them by *who writes them* rather than
   by what they are — removed the collision and the variable with it.

   To put the TSDB on another disk, make `central/prometheus` a symlink or a
   mount point. That was the only case `DATA_ROOT` was reachable for.

   **The cost, accepted:** `git clean -fdx` in the repo would delete the
   Prometheus TSDB along with every other gitignored file. Plain `git clean -fd`
   would not — only `-x` removes ignored files.

   **Images are pinned, not `:latest`.** Nothing pulls on a schedule now, so
   `:latest` would mean the running build is whatever was in the registry the
   last time someone ran `up -d`, with no record of which. `publish-images.sh`
   prints the sha to paste into `.env`; rolling back is editing one line.

   This makes **C2 load-bearing**: nothing converges a missed node overnight, so
   a node forgotten during a rollout stays stale indefinitely.
   `sparkdash_agent_build_info` and the `AgentBuildSkew` alert are the only
   things that would notice — and a stale agent has twice presented as a missing
   *feature* rather than a stale agent.

   **Build on one node, not all three.** All GX10s are arm64 and the image
   carries nothing node-specific (`NODE_ID` comes from the host's hostname at
   runtime), so `publish-images.sh agent` should run on exactly one of them. Two
   nodes each building and pushing the same tag would leave the second
   overwriting the first with a **different digest under the same tag**, and
   nodes would then run different bytes depending on when they pulled. The
   clone on the other nodes exists for `validate-on-gx10.sh` and diagnostics,
   not for building.

   **Config-only changes take effect on reload, not recreate** — settled by
   moving `prometheus.yml`, `alerts.yml` and `alertmanager.yml` into `config/`
   as a **directory** mount. A single-file mount follows the inode, and `git
   pull` replaces files rather than editing them, so the container went on
   reading the old inode while a reload reported success — observed 2026-08-15
   while deploying C2. A directory mount resolves each entry on access, so a
   pull plus `docker kill -s HUP sparkdash-prometheus` is enough.

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
   and recreated freely, and [../node/README.md](../node/README.md)
   leans on exactly that property. A database means a schema, migrations,
   corruption modes, and a second thing in the backup set — softened only by the
   monitoring VM already carrying the TSDB, so backup discipline isn't starting
   from zero.

   **Sequence: A7, then B, then reassess.** B may satisfy enough of the
   fault-analysis need that the snapshot log stops feeling necessary — and that
   is the outcome to hope for, since it costs nothing to maintain.
