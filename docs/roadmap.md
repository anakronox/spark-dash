# Roadmap

Phased so that we get a useful dashboard on the single existing node quickly,
before the other 2 GX10 units even arrive.

**Status as of 2026-08-28.** All three nodes are live — `sparky` standalone plus
the `danflashes` pair — and the phases below are shipped. Checkboxes are trued
up against the deployed system rather than against memory.

**Three markers are in use:**

| | meaning |
|---|---|
| `[x]` | shipped, or answered |
| `[ ]` | open — genuinely still to decide or do |
| `[~]` | **retired**: not done, and deliberately not going to be. The reason is written where the item is, because an item deleted outright comes back as somebody's good idea six months later. |

Swept 2026-08-28: ten open items became three. Most were Phase-1 leftovers
overtaken by later sections or by the project going public, and two were
finished without being ticked. What remains open is open on purpose — `V2b` and
`X4` are deferred decisions with their triggers recorded, and `AB2` is a
write-up rather than a build.

## Phase 0 — Project setup

- [x] Requirements, architecture, metrics, deployment, and app-design docs.
- [~] Mirror phases below as Forgejo issues/milestones for tracking.
  **RETIRED 2026-08-28.** This file is the tracker, and the project is public on
  GitHub now — issues point there. A second copy in Forgejo would be a third
  place for the same list to drift.

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

- [x] Roll out the same per-node stack to nodes 2 and 3. **Done** — `sparketa`
  and `sparkjr` run the identical stack; the only per-node value is the
  hostname the agent reads for its own id.
- [x] Move Prometheus target list to file-based service discovery. **Done** —
  `prometheus.yml` uses `file_sd_configs` against `targets/`, rendered by the
  backend from `cluster.yml` with a 30s refresh. Adding a node needs no Prometheus config
  change and no restart.
- [x] Extend backend/frontend to aggregate across nodes — `/api/cluster/summary`
  and the per-node health cards exist and handle clustering.
- [x] Point the central Prometheus at all 3 nodes. **Done** — rendered from
  `cluster.yml` into `targets/generated/`, so adding a node is one edit and
  no Prometheus restart.

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

- [x] Confirm dashboard works through the Cloudflare Tunnel + Google OAuth path.
  Answered by use: the dashboard has been reached that way daily for weeks.
- [~] (Optional, defense-in-depth) Validate `Cf-Access-Jwt-Assertion` in the
  backend. **RETIRED 2026-08-28** — never started, and the threat it addresses
  (someone reaching the origin directly, bypassing Access) is a network
  question rather than an application one. Reopen it as a security item if the
  origin ever becomes reachable off-tunnel.
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

Sections are lettered in the order they were opened, not by priority. A–Z are
used; later ones continue AA, AB, and so on.

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
profiling counters (`DCGM_FI_PROF_DRAM_ACTIVE`), and **that route was closed
2026-08-21: DCGM will not ship.** Memory bandwidth is therefore a known,
accepted blind spot rather than an open question — see
[H](#h--genericize-for-distribution) for the reasoning and for what a spike
would look like if it ever becomes live. **Do not propose NVML for this
again.**

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
- [x] **E5. Shipped 2026-08-18.** `/api/annotations` and a marks layer on the
  history charts, with an `events` toggle carrying the count.

  **The filtering IS the feature, so it lives in one place.** One request, not
  three: the charts get a list of instants and do not decide what deserves to
  be one. Measured on a real 7-day window, drawing everything is 173 events on
  a ~390px chart — one every 2.3px, which is not an annotation layer but a wash
  that hides the data underneath it.

  The filter asks which events could plausibly EXPLAIN a change in the line. It
  is not a density cap chosen to fit:
  - alerts that FIRED — a pending-only episode means a mistuned rule, not an
    event on the hardware
  - COLD model starts — a warm sleep/wake costs almost nothing; a cold start
    reads weights back off disk and is what shows up as a latency spike
  - DEPLOYS, from the first appearance of each `sparkdash_agent_build_info`
    build label. A metric that changes shape right after the agent was replaced
    is a different story from one that changed on its own, and it is the first
    thing to rule out.

  A build whose first sample sits at the window start was already running and
  did not deploy inside it — otherwise every chart gets a phantom marker on its
  left edge, every time.

  **Marks are drawn BENEATH the series** (`drawClear`, not `draw`), recessive,
  and told apart by dash. Only alerts take a status colour, because that
  palette is reserved for exactly this and so cannot collide with node identity
  from `--chart-N`; a deploy is not a warning and must not borrow one. Hovering
  names the event in the existing tooltip, which is the whole point — the dip
  and its candidate cause in one glance instead of three views and mental
  alignment.

  **It exposed a real bug in alert episodes.** The first 7-day render drew a
  solid red band: 21 marks, all `InferenceTargetScrapeFailing`, spaced exactly
  one step apart. Not 21 incidents — ONE, re-detected at every sample.
  `DEFAULT_GAP_TOLERANCE_S` was 150s, about 2.5 evaluation intervals at a 60s
  step, and a gap cannot be smaller than the sampling resolution: at any step
  above 150s every sample looks like the start of a new episode.
  `fetch_episodes` documented that constraint in its own docstring and never
  enforced it. The tolerance now scales with the step, taking that window from
  34 marks to 14 with nothing repeated — and fixing the alert history view at
  coarse steps, where the same fragmentation was latent.
- [x] **E6.** Cluster outlier detection — **taken up as [Y](#y--straggler-detection-in-a-pooled-cluster-was-e6),
  where the premise is corrected.** Written as "same model, three nodes, one
  slower", which is not the shape the cluster took: `danflashes` runs ONE
  distributed model across two nodes, and only the head node reports throughput
  at all. The `cluster` label (C1) is still what makes the comparison
  expressible; what changed is that there is nothing per-node to compare on the
  inference side, so it has to be answered from hardware signals.

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
- [x] **G3. Shipped 2026-08-18.** `/api/targets/absent` and a dashboard notice
  for targets configured but long gone.

  **It takes over exactly where the alert gives up.**
  `InferenceTargetScrapeFailing` resolves after 24h on the assumption the
  endpoint was retired, which is right — an alert that cannot be cleared is one
  you learn to ignore. But "stops nagging" must not become "is forgotten", so
  the threshold here is the same 24h, deliberately.

  It reports what it CANNOT tell, which is the honest answer: a target down
  this long is either broken or deliberately gone, and nothing observable
  distinguishes them.

  **`timestamp()` must go INSIDE the subquery.** Applied outside, over
  `last_over_time`, it reports the time of the EVALUATION rather than of the
  sample — every target came back "last up 0 seconds ago", including one that
  was genuinely 32.5h down. The join is done in Python rather than PromQL
  because a target that has NEVER been up yields no series at all, and a PromQL
  join would silently drop exactly the typo'd port this exists to surface.

- [x] **G4. Shipped 2026-08-18.** `DELETE /api/targets/absent`, offered as a
  "retire" button on the notice above, writing through the same
  validate-and-replace path the settings editor uses.

  **Removal only, and inference only.** The line is environmental vs. scraped
  and it is not arbitrary: a GPU temperature or memory-pressure alert has no
  "retire" concept — the hardware still exists, and being able to delete those
  would let someone permanently blind the dashboard to a real failure. The
  endpoint refuses any job but `vllm` and says why.

  Matching is on the AUTHORITY, not the whole URL: Prometheus names an instance
  `host:port` while the config holds a URL, so comparing strings would make
  retire a silent no-op — the button appears to work and the target comes
  straight back.

  Rebuilt rather than mutated, because `ClusterNode` and `NodeRuntimes` are
  frozen dataclasses. Two mistakes on the way to that: `node.vllm` does not
  exist (it is `node.runtimes.vllm`), and `llama_routers` holds objects while
  `vllm` holds plain URL strings — assuming both were the same shape was a 500.

  **It did not work in production, and the reason was a design error.**
  Reported the same day: retire cleared the "not answering" banner and the
  "configured but absent" one came straight back.

  G4 says retire a SCRAPE TARGET. What shipped removed the endpoint from
  `cluster.yml`, which is the AGENT's polling config — so the agent stopped
  polling (the F7 banner cleared, correctly) while Prometheus went on scraping
  from `config/vllm-targets.yml`, a separate hand-maintained file. Two
  independent sources for one fact, and the button could only ever fix half of
  it.

  Fixed by removing the second source rather than writing to it: vLLM scrape
  targets are now GENERATED into `targets/vllm.yml` from `cluster.yml`, exactly
  as `agents.yml` and `node-exporters.yml` already were, and `prometheus.yml`
  reads the generated file. Writing to the hand-maintained one was rejected on
  the project's own rule — it is git-tracked, so a backend write would conflict
  on the next pull.

  **And a pre-existing bug the same investigation turned up:**
  `sync_prometheus_targets()` was only ever called at STARTUP. Both the retire
  path and the settings save invalidated the inventory cache and left the target
  files alone, so a node added from settings got no scrape target until the
  backend happened to restart. Both paths now re-render.

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
- [x] **F5. Shipped 2026-08-18.** `/health` names nodes whose configured
  endpoints did not answer, read off the snapshot the agent already sends —
  it is the thing that tried and failed, so it already knew.

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

- [x] **F6. Shipped 2026-08-18.** The settings cluster panel tags each node
  with where its runtimes came from and when central last answered it.

  **Three states, not two.** `central` (managed, with an age), `env` (absent
  from cluster.yml, so on env by DESIGN) and `unreachable` (asking, getting
  silence, so on env by ACCIDENT). The last two look identical from the
  outside and want completely different responses.

  The timestamp is the last SUCCESS, never the last attempt. `RemoteConfig`
  advances its retry clock on failure too — correct, so a dead backend is
  retried on the TTL rather than every tick — and reporting THAT would have
  told the reader their edit had landed when the last thing that happened was
  a timeout.
- [x] **F7. Shipped 2026-08-18.** A dashboard notice beside the unmonitored
  one, and `sparkdash_endpoint_reachable{node,runtime,endpoint}` so the gap is
  alertable and historical rather than only visible while someone is looking.

  **The agent already knew and nobody was listening.** `LlamaRouterMetrics`
  has carried `reachable` for ages and nothing consumed it — not the backend,
  not the UI. vLLM was worse: an unreachable instance returned None and was
  dropped from the list, so a typo'd vLLM port was indistinguishable from a
  node that runs no vLLM.

  1 = answering, 0 = configured and silent, and the series exists for healthy
  endpoints too — otherwise `absent()` could not tell "not configured" from
  "not answering".

  Down nodes are excluded from both this and F5: every endpoint on a down node
  is unreachable, and listing each one buries the single fact that matters
  under a list of its consequences.
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
- [x] **F9. Shipped 2026-08-18.** A "copy yaml" control per node in the
  settings cluster panel, generating the block to paste into `cluster.yml`.
  Most of the convenience of editing with no write path.

  **The generator was wrong and looked right.** Its first version emitted
  `llama_routers:` and `vllm:` at node level rather than under `runtimes:`, and
  `- port: N` for vLLM where the schema wants a bare number. That is valid YAML
  which loads as a node with NO runtimes — so pasting it would have appeared to
  work and then silently collected nothing, produced by the very tool meant to
  prevent that. Caught by round-tripping the output through the real loader,
  which is now a test that fails if the schema and the generator drift apart.

  **The clipboard is the shortcut, not the feature.** `navigator.clipboard`
  needs a SECURE CONTEXT and this dashboard is served over plain http on a LAN
  address, so the API is not merely permission-gated there — it is undefined.
  A button whose only behaviour was to copy would have worked perfectly on
  localhost and done nothing at all on the real deployment. Verified:
  `isSecureContext` is true on 127.0.0.1 and false on
  `http://192.168.50.156:8080`. The block is shown and pre-selected, which
  always works; the copy is attempted quietly alongside.

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
  [metrics.md](metrics.md#the-cluster-label-and-why-totals-are-usually-wrong),
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
- [x] **L2 — server state that cannot reach outward. CLOSED 2026-08-21 as
  absorbed, with nothing left to build.**

  L2 was a *tier* with a test, never a feature: a silence *"cannot repoint an
  agent, load a model or touch a process"*, and any candidate had to pass that
  same sentence. Reviewed against what now exists, every qualifying piece has
  shipped — it just arrived under other letters, which is why this checkbox
  outlived its contents.

  | what | arrived as |
  |---|---|
  | Alertmanager silences — the original exemplar | [G](#g--clearing-an-alarm) |
  | `cluster.yml` runtimes and ports | [L3](#l--a-settings-fly-out) |
  | Interface alert exclusions | [W](#w--choosing-which-interfaces-are-monitored--shipped-2026-08-21) |
  | Engine endpoints (vLLM, SGLang) | [V](#v--more-inference-runtimes-sglang-and-atlas--shipped-2026-08-21-v2b-deferred) |

  **What is left over either fails the test or is a trap**, and is recorded
  here so it is not proposed again:

  - **Alert thresholds.** Already flagged below as a trap and it has only got
    truer: [A](#a--alerting-correctness)'s finding was that hardcoded
    thresholds were the bug, and [Z3](#z--distributed-inference-is-one-workload-not-n-nodes)
    repeated the lesson from the other direction — a *level* that cannot be
    cleared. A field where someone types 80°C re-introduces exactly what made
    `GpuTemperatureCritical` unable to fire. Offsets from the derived value
    would be defensible; nothing has asked for them.
  - **`PROM_RETENTION`.** A container environment variable. The backend cannot
    write it and Prometheus needs a restart to read it, so it is not reachable
    from a settings panel at all — not a decision, a fact about where it lives.
  - **The ntfy URL.** A credential, deliberately kept in a file outside git so
    it is absent from the published surface. Writing it from the UI inverts the
    reason it lives there.
  - **Poll intervals, TTLs, timeouts.** Tuning knobs nobody has wanted to
    touch, and each one exposed is a way to make the dashboard worse with no
    way to notice.

  **One control already sits on the line, and it arrived without being weighed
  against this sentence.** The cluster editor ships a `scrape_metrics` checkbox
  per router (L3). Enabling it has the agent issue `/metrics?model=`, which
  **loads the model** on an autoload router — the one dashboard action that
  reaches into a node and changes its state, and a plain failure of "cannot
  load a model" read literally.

  What saves it is a gate elsewhere: the agent only scrapes models NVML reports
  as BUSY, so an idle model is never woken and its router's sleep timer expires
  normally. The capability is real, the composed behaviour is safe, and the two
  facts live in different files. Recorded here because the test is stated as
  absolute, and the next person to check a candidate against it should know
  that the existing set clears it by a mechanism rather than by construction.
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

**L3 needed an answer before a form, and got one — recorded because the
options were weighed and two were rejected.** This paragraph predates the
shipped L3 above and read as open long after it was settled.

  1. **Read-only display plus a copy-to-clipboard snippet.** Covers most of the
     real need with no write path at all — and it already exists as F6/F9, which
     is why it was not enough on its own: retiring a decommissioned endpoint
     still meant an SSH session.
  2. **Write, but constrain the value space.** ← **chosen.** Ports rather than
     URLs, hosts validated against RFC1918, off-node runtimes shown read-only.
     Turns an arbitrary-URL primitive into a much narrower one without refusing
     the feature.
  3. **Write, but not through the tunnel** — bind the mutating endpoints to the
     LAN only. Rejected as a second listener and compose work for a property
     option 2 already buys, on a service that sits behind OAuth at the edge
     regardless.

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

- [x] **M1b. Closed 2026-08-18 — satisfied by M1, and partly overtaken by it.**
  This was the ORIGINAL sorting note, written before M1 was scoped; M1 then
  shipped the same thing more broadly. Kept as a record rather than deleted,
  because the constraint it insisted on is the one worth remembering.

  Every requirement is met: `tok/s` is sortable in the Models table, sorting is
  client-side, and the third click of the cycle returns to the table's own
  deliberate order. Verified live — `aria-sort` cycles descending, ascending,
  none, and the row order comes back identical.

  **Its premise is now stale in a good way.** M1b says the default groups
  "by node then router", so finding what is serving needs a sort. That order
  changed: the table now leads with ACTIVE — "what's serving right now is what
  you came to see" — so the question M1b was written to answer is answered by
  the default, with no sort at all. Sorting by tok/s remains available for
  ranking among several that ARE serving.

  **One thing genuinely unverified, and it cannot be faked.** With the cluster
  idle every `tok/s` cell is `—`, so a sort on that column cannot reorder
  anything; nulls sort last in both directions by design. The sort MACHINERY
  was verified across all 32 columns with real value deltas when M1 shipped, but
  the specific "rank the busy models" outcome needs live inference traffic to
  demonstrate. Worth a glance the next time something is actually generating
  tokens.

  Note an active model legitimately reports no throughput: the agent only
  scrapes models NVML reports as busy, so an active-but-idle one is left alone
  and its tok/s is absent rather than zero. That is the behaviour, not a gap.

- [x] **M2. Filtering, as one interaction.** Shipped 2026-08-21.
  `lib/focus.svelte.ts` holds it: one store, every table reads it.

  **Scope by clicking a node's name**, which scopes the Models, GPU process and
  both Network tables at once. Clicking it again clears — the control that sets
  a filter has to unset it, or the only way back is a reload.

  **A button on the name, not a click handler on the card**, which deviates
  from the sketch above and is better. In compact mode the card is already
  `role="group"` with a tabindex for the hover-reveal; layering a second
  interaction onto that element would make a group clickable and give Enter two
  meanings. A real button gets keyboard access, an accessible name and pressed
  state from the platform, and the name is the obvious target anyway.

  **Filtering happens at the source**, before rows are built, so the header
  count ("3 active · 2 sleeping") agrees with the table. Filtering afterwards
  would leave the summary describing rows that are not there.

  **The page says it is scoped, and that is not decoration.** Without the
  banner a filtered page is indistinguishable from a cluster that lost two
  nodes: every table short, every count low, nothing explaining why. Same
  unrecoverability rule as hidden sections and hidden columns — whatever
  removes things from the page has to be visible ON that page with the way back
  attached. It carries neutral ink rather than the warning colour, because
  being scoped is a state you chose, not a problem.

  **Scoping to a node that then leaves `cluster.yml`** is called out by name
  ("...which the cluster no longer reports"), rather than silently rendering
  four empty tables. A node that is merely DOWN still has rows worth showing
  and is untouched by this. Same lesson as clamping the pager index when the
  row count moves.

  **NOT persisted, deliberately.** A filter is where you happen to be looking,
  not how you like the page. Returning tomorrow to a dashboard silently showing
  one node of three is the same failure as a silence outliving the memory of
  setting it. Theme and section order persist; this does not.

  **`loaded only` on the Models card** handles the other half — 32 of 36 rows
  were `unloaded` at four nodes. A toggle rather than a general predicate,
  because "is it actually loaded" is the only row filter anyone has wanted.
  `sleeping` survives it: a slept model holds a process and comes back fast,
  which is operationally different from cold. It takes the **funnel glyph** M4
  deliberately left unspent, and stays visible while active for the same reason
  a hidden column does.

- [x] **M3. Two columns that were already decided, and never wired up.**
  Closed 2026-08-21 — as a bug, not a feature.

  M3 asked what else deserves a column. The audit answered something else
  first: **`size` and `load` had shipped as dead code.** T1 added the size cell
  snippet, its sort value, its tooltip helper and its CSS; T2 did the same for
  load, including a fetch to the load-times endpoint on every poll. Neither
  added the `ColumnDef`. The rows are driven by that array, so both columns
  never reached the DOM — two roadmap items, reviewed and documented, rendering
  nothing for two days.

  **It is M4's hazard one step quieter.** There the fear was the header list and
  the cell list disagreeing about ORDER, which shifts every value sideways and
  looks like corrupted data. This is the same two lists disagreeing about
  EXISTENCE, which looks like nothing at all — no error, no blank column, just
  a feature that is absent.

  `tests/test_table_columns.py` now checks all three lists against each other
  across every table, in all three directions: a renderer without a def is an
  invisible column, a def without a renderer is a blank one, and a def without
  a sort value is a header button that does nothing when clicked. Removing the
  `size` line reproduces the original bug and fails the test.

  **Nothing else was added, and M3's original argument is why.** With size and
  load restored the Models table carries 11 columns, the process table 8. The
  remaining uncollected-but-unshown fields are `encoder_pct`/`decoder_pct`
  (zero on every node here — no transcoding workload), `physical_state` (the
  `state` column already says ACTIVE/DOWN) and `raw_status` (already the
  `state` cell's tooltip). Adding them would be accumulating columns rather
  than choosing them, which is the thing M3 was written to resist.

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

**The settings placement control, reworked 2026-08-18.** It cycled
full -> left -> right and had three faults, all reported as "inconsistent, and
it doesn't seem to refresh":

- **It silently reordered the layout.** `place()` appends to the end of the
  target zone, so a round trip was not a round trip: a section at the TOP of the
  left column came back at the BOTTOM. That quietly damaged an arrangement built
  by dragging, which is the worst of the three.
- **It could not be aimed.** With no natural order among three zones, every
  click's destination had to be memorised rather than predicted.
- **Its effect was invisible.** The fly-out covers the page and the list was
  flat, in `layout.order`, so the panel looked identical whatever the
  arrangement was.

Now two states — full or half — and the fix for the reordering needed no new
state at all. `order` is ONE list for the whole page and a zone's contents are
that list filtered by `placement`, so a section's position among its
column-mates is already recorded there. `setZone` changes only the placement and
leaves the order alone; a section sent full and back lands exactly where it was.
`place()` still rewrites the order, because a drag has to say where in the
target column the section goes — the settings toggle has no such opinion, and
taking one was the bug.

`half` returns a section to the column it last occupied, falling back to the
emptier column only for one that has never been in either. Guessing the emptier
column every time would silently move a section you deliberately put on the
right.

The division of labour that follows: **settings answers wide-or-narrow**, which
is the part you can decide without looking at the page; **which column and where
in it belongs to the drag**, because it needs the page in front of you. The list
is now grouped by zone in dashboard order, so the row moving between groups is
the feedback the panel previously could not give.

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

### P — Agent footprint — **first pass 2026-08-17**

The node stack is deliberately two containers with no persistent state so the
box stays free for models. That premise is worth checking rather than assuming,
and the GB10 makes it sharper than usual: there is ONE memory pool, no separate
VRAM, so every megabyte the monitoring agent holds is a megabyte a model cannot
have.

Measured on `sparky` before and after, agent container:

| | before | after |
|---|---|---|
| `docker stats` | 218.2 MiB | **81.3 MiB** |
| cgroup `anon` | 138.9 MB | 72.8 MB |
| cgroup `slab` | 77.9 MB | 2.5 MB |
| process RSS | 151.6 MB | 90.4 MB |
| threads | 11 | 7 |

Two changes, neither of them code:

- **`MALLOC_ARENA_MAX=2`.** Glibc opens a malloc arena per thread, up to 8 x
  cores, and each keeps its freed memory rather than returning it. Ten threads
  on a 20-core Grace is the shape that punishes hardest. Two rather than one
  because a single arena serialises every allocation and the collectors run
  concurrently.
- **`uvicorn` without `[standard]`.** That extra pulls uvloop, httptools,
  watchfiles, websockets and python-dotenv — machinery for a busy web service,
  on an agent serving three read-only GETs at a 2s poll and a 15s scrape. It
  also accounts for the four threads that went. `uvicorn.run()` passes no
  `loop=` or `http=`, so nothing changed in code; it falls back to asyncio and
  h11.

**Do not read the slab row as a 97% win.** Slab is kernel dentry and inode cache
accumulated by polling hundreds of `/proc` and `/sys` files, and the "after"
figure is a container that had been up for a minute against one that had been up
26 hours. It will regrow. It is also RECLAIMABLE — the kernel evicts it under
pressure, so it never actually denied memory to a model. The honest headline is
the anon and RSS rows, and even those flatter the new build slightly for the
same warm-up reason. Re-measure after a day before quoting a number.

**Still on the table, not done:** `nvitop` is a TUI monitoring library used for
`NA`, `Device` and `libnvml`, which `nvidia-ml-py` provides directly. Likely the
largest remaining win, but `collectors/gpu.py` is 400+ lines built around
nvitop's sentinel and process view — exactly the code where a subtle regression
stays invisible until a reading is wrong. Worth doing against the `diagnose`
command as a cross-check, and worth re-measuring first: at 81 MiB the agent is
now smaller than `llama-lab-router` (187 MiB) and a fifth of `backrest`
(486 MiB), so the case for spending that risk has weakened.

### Q — The agent goes dark while a model loads

**Reported 2026-08-18**, after a Hermes group-chat agent autoloaded Cydonia on
a router that was already serving Qwen: the sparky node dropped off the
dashboard entirely, the Cydonia card sat in a loading state for a long time,
and it recovered on its own.

**Measured, not inferred.** `scrape_duration_seconds{job="spark-dash-agent"}`
across the incident window:

| time (UTC) | scrape duration |
|---|---|
| 00:45:30 | 0.066s |
| 00:46:00 | **10.000s** |
| 00:46:30 | **10.000s** |
| 00:47:00 | 2.63s |
| 00:47:30 | 3.06s |
| 00:48:00 | 0.045s |

10.000s is Prometheus's scrape timeout exactly, so the true stall is "at least
ten seconds", not ten. The agent did **not** restart — a restart gives
connection-refused and resets counters; this is a read timeout against a
process that is up and not answering. Five such episodes in 48h, 36 down
scrapes, the two longest lasting 3 and 7 minutes.

**The 2.63s and 3.06s samples are the reason it looked so much worse on the
dashboard than in Prometheus.** `agent_timeout_s` is 3.0s, so the backend's
live poll was still timing out through the whole recovery tail, after
Prometheus had already gone green. The node keeps reading as unreachable for
as long as collection stays above 3s.

**Root cause, in two parts.**

1. `SnapshotCache.get()` holds its lock across `self._builder.build()`
   (`agent/src/spark_dash_agent/app.py`). There is no serve-stale-while-
   refreshing path, so every reader — Prometheus, the backend's live poll,
   anything else — waits for the *full* collection rather than getting the
   0.75s-old snapshot that is already in hand and would be perfectly good.

2. `build()` does its runtime HTTP **sequentially**, and the timeouts are
   per-request rather than per-build. Per router that is `/v1/models`, one
   `/metrics?model=` per active model, and `/props` — at 2s each, across two
   routers with three models on one of them, worst case is ~20s. httpx applies
   the 2s to connect and read separately, so a router whose accept backlog is
   full during a load can burn 4s on a single call. The arithmetic matches the
   observed ≥10s comfortably.

The per-request timeout is the part that scales wrong: every router, model and
vLLM endpoint added extends the worst-case stall linearly, and C (multi-node)
multiplies the number of agents this can happen to.

- [x] **Q1.** Serve the cached snapshot immediately and refresh in the
  background (single-flight, so a burst still collapses onto one collection).
  A reader then never waits on collection at all. Only the very first call
  after start has nothing to serve.
- [x] **Q2.** Give `build()` a total deadline, not just per-request timeouts,
  so the worst case is bounded by a constant instead of by how many runtimes
  the node happens to serve. A collector past the deadline reports as
  unreachable for that tick, which is what it already does on timeout.
- [x] **Q3.** Collect routers (and vLLM endpoints) concurrently. Bounds the
  wall time at roughly one slow endpoint rather than their sum, and is what
  makes Q2's deadline generous rather than tight.
- [x] **Q4.** Expose collection duration as a metric. This incident was only
  legible because Prometheus records `scrape_duration_seconds` for its own
  scrape; the agent has no view of how long its own collection takes, so the
  slow path is invisible until a scrape fails.

**Not a fix: raising `agent_timeout_s`.** It would paper over the symptom, cost
live-view latency on every genuinely dead node, and still fail once a node
serves enough runtimes. With Q1 the agent answers in microseconds and 3s is
generous.

**Unverified: the exact empty state the dashboard showed.** `live.svelte.ts`
does not clear `snapshot` on disconnect (`state = this.snapshot ?
'reconnecting' : 'offline'`), so the expected behaviour is a stale view rather
than an empty one, and `fetch_node` synthesises an `up=false` node rather than
omitting it. Reproducing "no nodes loaded" needs either a page load during the
stall or a frame that genuinely carried an empty node list. Worth pinning down
before assuming Q1–Q3 fully cover what was seen.

**Shipped 2026-08-18.** Four things the build turned up that the diagnosis had
not:

- **A burst did not merely persist the stall, it amplified it.** Running the
  new tests against the old cache, twenty concurrent readers produced
  **twenty-one sequential collections** — every waiter acquired the lock, found
  the snapshot stale again (it had been stale when it started queuing), and
  collected afresh. So the failure got worse exactly as more consumers arrived,
  which is the opposite of what the TTL was there to do.

- **Stale-while-revalidate on its own was the wrong fix**, and building it
  showed why. If a stale snapshot is always returned immediately, then
  Prometheus — usually the only caller, scraping every 15s — records data
  collected 15s before the timestamp it is stored under. A systematic
  one-interval lag across all history is too high a price for stall immunity.
  `get()` therefore waits a short grace period (0.25s) for the refresh before
  giving up and serving what it has: collection is ~80ms when the routers
  answer, so the normal case stays exact, and a stalled one is served stale
  well inside the backend's 3s and Prometheus's 10s.

- **Q1 silently removed an alarm, so Q5 was needed to put it back.** A stalled
  agent used to fail its scrape, so `up` went 0 and `NodeAgentDown` fired. The
  agent now answers through a stall — which is the fix — and that made the
  symptom invisible. Freshness has to be *reported* now rather than inferred
  from the scrape succeeding.

- **`httpx.MockTransport` ignores timeouts**, so the first budget test passed
  5.01s against a 1s budget and looked like a bug in the budget. It was a bug
  in the test. Replaced with two that can actually fail: one asserting the
  timeout httpx was *handed* (`request.extensions["timeout"]`), and one against
  a real socket that accepts and never writes — which is what a router loading
  a model actually does, and why a connect timeout never fired.

- [x] **Q5.** `AgentSnapshotStale` — alerts on
  `sparkdash_agent_snapshot_age_seconds > 30` for 2m. Validated with promtool.

**Measured after the change**, on the collectors themselves: four routers that
accept and never answer cost **0.75s with the budget against 2.02s without**
it, and the concurrency test shows four slow routers costing about one
router's time rather than four.

**Verified on sparky, 2026-08-18**, by reproducing the fault rather than
waiting for it. A socket bound on the node accepted connections and never
answered — which is what a router does while it loads a model, and why no
connect timeout ever fired — and old and new agents were pointed at it in turn,
same box, same fault, alongside the untouched production stack:

| | old agent | new agent |
|---|---|---|
| `/snapshot` latency | 4.08 – 7.15s | **0.25s** |

4.1s is already past the backend's 3.0s poll timeout, so that column IS the node
dropping off the dashboard. Note this was with **two routers and zero models**;
the real incident had three models on one router, which is where ≥10s came from.

On the new agent the split the fix is supposed to produce is visible directly:
collection cost **2.10s** while `/snapshot` answered in **0.25s** — the grace
period exactly. Against the real healthy routers, collection is 93ms and the
served snapshot is 1.4ms old, so the grace period keeps the normal case fresh
rather than trading it away.

Also confirmed there: `collection_stalled` stayed 0 throughout, correctly —
2.1s collection with a 0.44s snapshot age is slow-but-working, not a stall.

### R — Closing out Q, and getting ready for nodes 2 and 3

Two threads, and the second one has a date on it: **the hardware is expected
within 2 days of 2026-08-18.**

#### Loose ends from Q

- [x] **R1.** Reproduce or disprove "no nodes loaded". Q fixed the stall, but
  never explained the empty state actually reported. On reading, it should not
  be reachable: `live.svelte.ts:87` deliberately keeps the last snapshot on
  disconnect (`state = this.snapshot ? 'reconnecting' : 'offline'`), and
  `fetch_node` synthesises an `up=false` node rather than omitting one — a
  missing tile is easy to overlook, a red one is not.

  **Settled 2026-08-18: disproved.** The agent was stopped on sparky for 75s
  with the dashboard open in a browser, and watched.

  A down node does not disappear. The card stays and turns red — `critical
  unreachable`, `no runtimes`, `No data. Last seen now. ConnectError: All
  connection attempts failed` — with `NODES UP` reading `0/1` in red. Reloading
  the page *during* the outage renders the same red card, not an empty page: a
  refused connection errors instantly, so the first frame arrives immediately.

  So the reported wording was almost certainly that state rather than an empty
  node list. The card itself says "no runtimes", the header says `0/1`, and the
  Models panel says "No models registered" — any of which reads as "no nodes
  loaded" in recollection. During a *stall* rather than a stop, a page load
  would additionally sit on "Waiting for the first frame…" for up to the
  backend's 3s node timeout, since a stalled connection is accepted rather than
  refused.

  Q was the whole answer. Nothing further to fix here — but the investigation
  turned up R8.

- [x] **R2.** Alert on collection *failing*, not just aging. Q4 exports five
  metrics and `AgentSnapshotStale` consumes one. A rising
  `sparkdash_agent_collect_failures_total` means `build()` is raising, and
  right now that is silent by design — the previous snapshot keeps being served,
  which is correct behaviour and invisible.

- [x] **R4.** Re-measure the agent footprint over a full day. P's numbers
  (81.3 MiB `docker stats`, 90.4 MB RSS) predate Q's background refresh thread
  and its per-collect `ThreadPoolExecutor`s. First reading after deploy was
  71.2 MiB, which looks fine — but the container was two minutes old, and P
  exists because glibc arenas grow with thread count over time. Threads on the
  hot path are exactly the thing that section measured, so confirm rather than
  assume.

  **Measured 2026-08-19 after 15.5h uptime. No regression — slightly better.**

  | | P baseline | after Q |
  |---|---|---|
  | `docker stats` | 81.3 MiB | **75.9 MiB** |
  | process RSS | 90.4 MB | **87.2 MB** |
  | threads | — | 5 |
  | CPU | — | 0.33% |

  Fifteen hours is the part that matters: P exists because glibc arenas grow
  with thread count over time, so a two-minute reading proved nothing. Five
  threads after that long also rules out a leak — Q's background refresh thread
  and its per-collect `ThreadPoolExecutor`s are created and torn down per
  cycle, and a leak would show as thousands, not five. `MALLOC_ARENA_MAX=2`
  from P is still doing its job.

Deferred, recorded so they are not rediscovered:

- **R3.** Nothing compares deployment *intent* to *reality*. Both stacks ran
  `:latest` while their `.env` pinned a sha, unnoticed for some time. `/health`
  reports what is running; nothing reports what was meant to run. Possibly a
  doc or a check rather than code.
- **R5.** The Q4 metrics live only in Prometheus. A node card showing
  "collection 2.1s" would put the slow path where a reader is already looking.

- [x] **R8.** An EMPTY but valid `cluster.yml` silently drops every node, while
  an INVALID one correctly keeps the previous inventory. Found 2026-08-18 while
  disproving R1; backwards from the intent stated in the comment right above
  the code.

  `Inventory._load()` guards the cluster path with a truthiness check:

      try:
          cluster = load_cluster(self._cluster_config)
      except ClusterConfigError:
          return self._nodes          # typo -> keep serving. Correct.
      if cluster:                     # <- empty list falls THROUGH
          ...
      self._cluster = []              # and past here the file is "not in play"

  A file that parses to no nodes is not an error, so it falls past this into the
  `SPARK_NODES` env path and then the targets file, and can return `[]` — every
  node gone from the dashboard. A typo is survivable; emptying the file is not.

  Not what happened in the Q incident (`cluster.yml` writes go through
  `os.replace` from a temp file in the same directory, so a torn read is
  impossible). It is a hand-editing trap, which makes it **this week's problem
  specifically**: adding nodes 2 and 3 means editing that file, and commenting
  every node out mid-edit blanks the dashboard rather than holding the last good
  config.

  Fix is to treat "parsed, but no nodes" as the same class of event as a parse
  error when a cluster file is configured: keep the previous inventory and say
  so loudly. Deliberately emptying the cluster is not a thing anyone does.

  **Fixed 2026-08-18, and the fix needed narrowing twice.**

  First attempt guarded on "file exists but yields no nodes" and broke
  `test_empty_cluster_file_falls_through`, which pins a real behaviour: an
  empty file on a COLD start means "created but not filled in yet", and
  `SPARK_NODES` must still work. Empty means two different things depending on
  whether any node has been served yet, so the guard is gated on `self._nodes`
  as well.

  Then the new test passed against the unfixed code — worse than no test. Two
  reasons, both worth remembering: a fully-commented file parses to `[]` rather
  than raising, so it never reached the error branch; and `invalidate()` sets
  `_loaded_at = 0.0` while `nodes()` re-reads only past `ttl_s` (30s), so
  `nodes(now=1.0)` served the cache and never re-read at all. With `now=9999.0`
  it fails correctly against the old code — `assert [] == ['sparky', 'sparky2']`.

#### Multi-node readiness — do BEFORE the hardware arrives

The multi-node paths have only ever run against fakes and a stubbed history
response. The difference between nodes 2 and 3 joining quietly and costing a
debugging session is whether these are exercised first.

- [x] **R6.** Dry-run a second node on existing hardware. Q's verification
  already proved the technique: a second agent container on a spare port, with
  its own `NODE_ID`, added to `cluster.yml` for real. That exercises the whole
  chain — inventory, generated Prometheus targets, scrape, node cards, the
  chart legend and solo toggles, `AgentBuildSkew` — on real infrastructure
  rather than fixtures, with no new hardware.

  Removing it afterwards exercises the retire path (F/G) as well, which is
  worth having rehearsed before doing it in anger.

- [x] **R7.** **Append new nodes to `cluster.yml`; do not sort it.** Found
  2026-08-18 while planning. `nodeSlots()` is
  `new Map(nodeIds.map((id, i) => [id, i]))` — the slot is the node's POSITION
  in the ordered list, while the docstring above it says colour follows the
  node and not its position. Both are true of *filtering*, which is what that
  comment was written about, and neither is true of *insertion*: a node added
  anywhere but the end shifts every slot after it, so `gx10-2` landing before
  `sparky` alphabetically repaints the node you have been reading by colour for
  months.

  **Correction, 2026-08-18:** the above cites the wrong function.
  `nodeSlots()` in `theme.ts` is **dead code** — exported, never called. The
  live assignment is `slotOf` in `App.svelte:104`, a flat running count over
  the GROUPED node list:

      for (const c of clusters) for (const n of c.nodes) m.set(n.node_id, next++);

  Its own comment is honest about the limit — "the order it counts is stable
  *for a given cluster layout*" — and nodes 2 and 3 change the cluster layout.
  Per [[deployment-setup]] the plan is one standalone GX10 plus a dual-node
  cluster, so if that cluster renders before the standalone, sparky moves from
  slot 0 to slot 2 and changes colour.

  Decide which it should be. Appending is free and works today; keying the slot
  to a stable identity (hash of node id, or an explicit `slot:` in
  `cluster.yml`) survives any ordering. The palette itself is fine either way —
  8 slots, and `nodeColorVar` degrades to `var(--rule)` past that, so three
  nodes is comfortable. Delete `nodeSlots()` either way: two functions claiming
  to answer the same question, one of them unreachable and disagreeing with the
  other, is how the next person gets this wrong.

  **Decided and shipped 2026-08-18: the slot is the node's index in the
  INVENTORY**, i.e. its position in `cluster.yml`. A node's colour is its line
  in that file.

  Rejected: persisting the assignment in `localStorage`. It survives any edit,
  but makes a node a different colour on the phone than on the desktop, and
  colour is the thing you identify a node BY. Deterministic and shared beats
  stable-but-personal.

  Rejected: hashing the node id into a slot. Removes the ordering constraint
  and introduces collisions instead — two nodes the same colour is strictly
  worse than one reshuffle you chose.

  What this buys is an invariant a human can hold: **append to `cluster.yml`
  and no existing node changes colour.** Documented in `cluster.yml.example`,
  where someone editing it will see it, rather than only here. The chain is
  order-preserving end to end — `cluster.yml` → `inventory.nodes()` →
  `asyncio.gather` (which preserves input order) → the snapshot → the slot.

  The cost is that colours are no longer visually sequential once grouping
  reorders the cards. Correct trade: colour is identity, not rank.

  `nodeSlots()` was NOT deleted in the end. It turned out to be exactly the
  implementation wanted — `App.svelte` had simply never used it and had
  diverged. Made canonical and called, so the colour rule lives in one file
  beside `NODE_SLOTS` and `nodeColorVar`.

  **Untestable for now:** the frontend has no test framework (no vitest), so
  this rests on `svelte-check` and reasoning. R6's dry-run is what will actually
  exercise it, which is another reason to do R6 before the hardware.

  **Run 2026-08-19, and it paid for itself immediately.** A second agent
  (`NODE_ID=r6-dryrun`, sparky:9501, deliberately the older `effea27` build)
  appended to `cluster.yml`, then removed through the settings UI. Prometheus's
  admin API was enabled for the duration so the test series could be purged
  afterwards, and removed again.

  What worked, now exercised against two real nodes rather than fakes:

  - Two node cards, distinct colours, `NODES UP 2/2`.
  - The build-skew banner naming both builds, and `AgentBuildSkew` going
    `pending` with 1 active alert — a positive test of a load-bearing rule that
    had probably never fired in anger. (`for: 30m`, so it was never going to
    reach `firing` in the window.)
  - The **chart legend with two nodes**, and the solo toggle: clicking
    `r6-dryrun` dimmed `sparky`, showed a `1 of 2 · show all` control, and
    scoped every chart to the one node. This is the path O shipped and that had
    only ever run against a stubbed history response.
  - The unmonitored-runtimes banner correctly reporting llama.cpp on the new
    node.
  - The settings-save path re-rendering targets with no restart, and leaving
    sparky's config byte-identical to the pre-test backup.

  **What it found: R9.**

- [x] **R9.** A HAND EDIT of `cluster.yml` never re-rendered Prometheus's
  targets. `sync_prometheus_targets()` ran at startup, on retire, and on a
  settings save — nowhere else. So the node appeared on the dashboard within
  the 30s inventory TTL and was *never scraped*: live view and history
  disagreed, silently, while the dashboard looked entirely correct.

  This is how the README teaches adding a node, and `cluster.yml`'s own
  generated header claims it: *"Editing this file by hand still works and is
  picked up on the backend's next read."* True of the live view, false of
  Prometheus — the file documents the gap.

  Rendering was never broken; only the trigger was missing, which is why
  restarting the backend "fixed" it and made this easy to miss. Confirmed both
  ways during the dry-run: no targets after the edit, correct targets
  immediately after a restart.

  Fixed in `Inventory.nodes()` — when a reload produces a different list, the
  targets are re-rendered there, so every present and future path that changes
  the node list is covered by construction rather than by remembering. Guarded
  against re-entrancy, because `sync_prometheus_targets()` calls `nodes()` and
  would otherwise turn its own write into a no-op and return False.

  **Directly relevant to this week:** adding nodes 2 and 3 by hand would have
  produced two nodes visible on the dashboard with no history and no alerting.

### S — Three signals already collected and not shown

Planned 2026-08-19. The theme: **none of this needs new collection.** Every
number below is already gathered, already in Prometheus, and in two cases
already alerted on — it just never reaches a reader's eye. That makes these
cheap, and it makes the work display and judgement rather than plumbing.

- [x] **S1. `temp_bands` — the scale for every temperature already shown.** — shipped 2026-08-19

  Carries `gpu_warning_c`, `gpu_critical_c`, `cpu_warning_c`, `cpu_critical_c`,
  and a `*_source` for each. It is **not in `frontend/src/lib/types.ts` at
  all** — the field never crosses into the UI.

  The node card says `TEMP 47°C` with nothing to compare it against, so a
  reader cannot tell 47 from 84 without knowing the hardware. That is exactly
  what this field exists to prevent: it was added so alerting compares against
  *the node's own* thresholds rather than numbers hardcoded in a rule file.
  The UI then hardcodes nothing either, which also keeps [J](#j--single-host-profile-everything-on-one-gb10)
  honest — a dashboard that hardcodes GB10 temperatures is a GB10 dashboard in
  a way this one does not have to be.

  `*_source` is the part not to drop. It distinguishes a hardware-derived band
  from a fallback guess, and a threshold you cannot trust must not be rendered
  like one you can. Show the band; mark it when it is a guess.

  **Shipped.** The GPU temperature on the node card is now toned against the
  node's OWN bands and carries them in its tooltip, so `56°C` finally has a
  scale attached. Nothing in the frontend hardcodes a temperature, which is
  what keeps [J](#j--single-host-profile-everything-on-one-gb10) reachable: a
  dashboard that hardcoded GB10 trip points would be a GB10 dashboard in a way
  this one does not have to be.

  **Toned with `>`, not `>=`**, matching `health.py`'s
  `temp_c > temps.critical_c`. A degree of disagreement between the card and
  the health pill about the same reading is worse than either being slightly
  conservative.

  **`gpu_source` turned out to be a provenance label, not a boolean**, which
  the first implementation got wrong. The real vocabulary is `nvml-slowdown`,
  `acpi-critical-trip`, `override` and `fallback` — the live node reports
  `nvml-slowdown` (82/86°C) and `acpi-critical-trip` (92.8/98.8°C). Checking for
  a literal `"hardware"` would have labelled a genuine NVML reading an
  "estimate". Only `fallback` is untrustworthy; the rest name where the number
  came from and are shown verbatim, because the label is more informative than
  any paraphrase. `fallback` is spelled out as "not read from this device",
  since a guess presented in the same voice as a measurement is precisely the
  failure this field exists to prevent.

  CPU bands are carried through to the frontend but not yet displayed — the
  vitals strip shows GPU temperature only. They are there for whenever CPU
  temperature earns a place.

- [x] **S2. Swap — and the premise needs correcting first.** — shipped 2026-08-19

  `swap_used_bytes` is a **level, not a thrash signal**. A node can hold
  gigabytes of cold pages swapped out and be perfectly healthy, while another
  thrashes badly at a hundred megabytes. Thrashing is a *rate*, and displaying
  the level as though it were the symptom would produce exactly the wrong
  reaction on a unified-memory box where some swap is normal.

  **The detection already exists and is good:** `SwapThrashing` fires on
  `rate(node_vmstat_pswpin[5m]) + rate(node_vmstat_pswpout[5m]) > 50` for 10m,
  and its own description notes that PSI usually catches it too and that this
  rule "names the mechanism". Nothing to build there.

  What is missing is **everything between zero and the alert threshold.** The
  dashboard shows no swap at all, so the state is invisible until a warning
  fires, and there is no way to see it trending toward one.

  Three pieces, in increasing order of value:

  1. `swap_used_bytes` on the node card — already collected, honest if labelled
     as a level rather than as trouble.
  2. A **swap I/O rate** chart from `pswpin + pswpout`. Two lines
     (`HISTORY_QUERIES` + a `METRICS` chip), no collection, and it plots the
     exact quantity `SwapThrashing` alerts on — so the chart and the alert
     cannot disagree about what thrashing is.
  3. **`psi_memory_full` is collected, exported, and never charted.** The agent
     emits `psi_memory_full_avg10` and `full_avg60`; only the `some_*` variants
     appear in `HISTORY_QUERIES`. `some` means *someone* stalled on memory;
     `full` means *everything* did. On a box whose whole job is holding models
     in one shared pool, `full` above zero is the strongest "this node is in
     trouble right now" signal available, and it is being thrown away at the
     last step.

  **Shipped, all three pieces, none needing new collection.**

  `psi_memory_full` is the one that mattered. It was already collected AND
  already exported; only a `HISTORY_QUERIES` entry and a chip were missing, so
  the strongest distress signal the agent produces was being discarded at the
  last step. Charted at avg10 to match its `some` sibling — the two are meant to
  be read against each other, and a `some` at 10s beside a `full` at 60s would
  invite comparing numbers that are not comparable.

  `swap_io` plots `rate(pswpin) + rate(pswpout)`, which is the exact expression
  `SwapThrashing` alerts on, so the chart and the alert cannot disagree about
  what thrashing is. No `scaleMax`: there is no natural ceiling and a fixed one
  would clip the event the chart exists to show.

  Swap occupancy went on the node card as a plain, untoned level. Colouring a
  non-zero value would assert trouble that a resident figure cannot establish —
  a node can hold gigabytes of cold pages and be perfectly healthy. It earns a
  place at all because this is a unified-memory box: swap in use means the one
  pool models live in is under real pressure, and nothing said so before.

  Both queries were run against production Prometheus before wiring anything,
  and `psi_full_avg10` was confirmed to accept an appended `{node=...}` matcher
  before being added to `NODE_FILTERABLE`.

  **Also added `tests/test_history_metrics.py`**, because this section added two
  entries to two lists in two languages — the shape that drifts. It asserts every
  chip has a query and every query is reachable, which is precisely the bug
  reported once as "the Throughput chip does nothing": a chip whose key had no
  query rendered a control that could neither show nor hide anything. Verified
  by adding a ghost chip and watching it fail. It also checks that everything in
  `NODE_FILTERABLE` really is a bare selector, since appending a matcher to an
  aggregation is invalid PromQL and surfaces as a 503 rather than an honest
  error.

- [x] **S3. Disk used vs free.** — shipped 2026-08-19

  Also already in Prometheus — `node_filesystem_avail_bytes` /
  `node_filesystem_size_bytes`, per node, per mountpoint. No collection needed.

  The work here is **filtering**, which is the real cost of any card. Measured
  2026-08-19, the two hosts report between them: `/boot/efi` (vfat), `/`
  (ext4), `/Volumes/AI` and `/Volumes/Backups` (both nfs4, and both reporting
  an identical 21391 GB because they are the same NAS mounted twice), plus on
  the VM a run of `ramfs`/`tmpfs` entries — `/run`, `/run/lock`, and four
  `/run/credentials/systemd-*.service` mounts, all 0 GB. A naive table is eight
  rows of noise around two useful ones.

  **Use the same `fstype=~"ext4|xfs|btrfs"` filter the alerts already use.**
  Not for tidiness: if the card shows a filesystem the alerts ignore, it
  promises a warning that will never arrive. Card and alert should agree on
  what counts as a disk.

  Two findings from checking this:

  - **`/models` is on the local ext4 root, not the NAS** — `/dev/nvme0n1p2`,
    3.6 TB, 61% used, with 894 GB of models on it. So model storage *is*
    covered by `NodeDiskFillingUp` and `NodeDiskLow`. Worth knowing before
    assuming otherwise; the mount names suggest the opposite.
  - **The 59 TB NAS at `/Volumes/AI` (67% used) is excluded** from both disk
    alerts by that same fstype filter. Defensible — it is shared storage with
    its own lifecycle, not this node's problem — but it is currently excluded
    by accident of a filter rather than by decision. Make it a decision.
    Separately, a *stale* NFS mount breaks model loading entirely and nothing
    watches for that at all; capacity is not the failure mode worth fearing
    there.

  **Shipped as Used/Total on the node card, plus a 90% warning tier.**

  Two design decisions worth keeping:

  **The agent could not see the host root at all.** It mounts `/proc`, `/sys`
  and `/etc/hostname` and nothing else, so `statvfs("/")` inside the container
  measures the image's own overlay — a different disk, plausible-looking and
  entirely wrong. Fixed with `- /:/host/root:ro` in `node/compose.yaml`.

  **Deliberately NOT `rslave`**, which the node-exporter service beside it does
  use. `rslave` propagates the host's submounts into the container, including
  the NAS at `/Volumes/AI`. `statvfs` on a stale NFS mount does not fail, it
  blocks uninterruptibly — and snapshot collection is the one path that must
  never hang ([Q](#q--the-agent-goes-dark-while-a-model-loads)). A plain bind
  exposes the root filesystem and nothing beneath it, which makes that failure
  impossible rather than merely unlikely. The collector reinforces it: one
  `statvfs`, one path, never an enumeration, and there is a test asserting
  exactly that.

  Cached at 60s. Disk fills over hours while the snapshot is built every couple
  of seconds, so the TTL bounds how often a filesystem is touched at all.

  **One definition of "full", in three places.** `used = total - available`,
  not `total - free`; the gap is the filesystem's reserved blocks. `available`
  is what `NodeDiskLow` already alerts on, so the card, the new
  `NodeDiskWarning`, and the existing critical all measure the same thing. A
  dashboard reading a few points lower than the alert about to fire is worse
  than no dashboard.

  `NodeDiskWarning` is a warning TIER at 90% for 30m, beneath the existing 95%
  critical rather than replacing it — 90% is when to go and look, 95% is when
  writes start failing. `for: 30m` because a model download can cross 90%
  briefly and come back. Currently sparky `/` is 62.7% and the VM `/` 16.3%, so
  it will not fire on arrival. Validated with promtool: 32 rules.

  Unit choice: **GiB, not auto-scaled to TiB**, per `format.ts`'s stated rule
  that one fixed unit beats unit-checking before two numbers can be compared —
  which is the case that matters once three nodes' disks sit side by side. The
  tone carries "should I care" (warning at 90, critical at 95, matching the
  alert tiers exactly), so the digits never need reading closely.

  **Deploying this needs the node stack recreated, not just a new image** — the
  bind mount is new. Until then the collector logs which mount is missing and
  the reading is absent rather than wrong.

**Suggested order: S1, then S2.3 (`psi_memory_full`), then the rest.** S1
gives meaning to a number already on screen. `psi_memory_full` is two lines for
the single best distress signal the agent produces. Both are pure display with
no new failure modes. S3's chart is equally cheap; S3 as a *card* is the only
item here with real design work in it, and it can wait for a reason to exist.

### T — Model load/unload times on the Models card

Planned 2026-08-19, out of the Cydonia incident: the question that morning was
"why is this taking so long", and nothing on the dashboard could answer it.

**Read the accuracy ceiling first, because it shapes everything else.**

Load duration has to come from *observed* state transitions. The agent's
one-hot `sparkdash_llama_model_state{node,router,model,state}` series already
records every transition and `timeline.py` already reconstructs them — but it
is scraped every 15s, so:

- A Cydonia-class load (tens of seconds) measures to ±15s. Useful.
- A 5s load is usually **invisible**: no sample lands while `state="loading"`,
  and the transition reads `unloaded → active` with nothing in between.

So this reports "about 75s", never "74.3s", and short loads legitimately show
nothing. Present it as approximate or it will be read as precise.

**The router cannot do better, checked 2026-08-19.** `/v1/models` carries a
`created` field, which looks like exactly the timestamp needed. It is not:
llama.cpp fills it with the response time, and all three models on the
production router returned `created` equal to `now` on the same request. There
is no load-start timestamp to be had, so the scrape interval is the floor.

- [x] **T1. Model size, parameters and quantisation — available now, discarded
  now.** Do this one first; it is what makes T2 interpretable.

  `/v1/models` already returns a `meta` block the agent parses past:

      "meta": { "n_params": 23572403200, "size": 16756101120,
                "ftype": "Q5_K - Medium", "n_ctx": 131072, ... }

  That is 23.6B parameters, **15.6 GiB resident**, Q5_K, 128K context — and
  `RouterModel` keeps none of it. It carries name, state, raw_status, slots,
  kv, tok/s, running and waiting: everything about the model's *activity* and
  nothing about the model.

  This is the correlate the whole item is for. A 15.6 GiB model taking 90s to
  load is ~175 MB/s, which is a **disk** answer rather than a mystery. Without
  size beside it, a load time is a number you cannot reason about.

  Free to collect: parse it in `_discover_models`, from a response already
  being fetched. No new request, and nothing that can wake a model.

  Size also earns its place independently — it is the per-model half of the
  unified-memory question that `MemoryBand` answers only in aggregate.

  **Shipped 2026-08-19.** One column, not three. `size` sits beside `state` —
  together they are "what is this model", separate from the activity columns —
  with parameters, quantisation and context window in its tooltip. M3's
  position is that columns are chosen rather than accumulated, and these
  tables' widths were hard-won; three new columns would have spent that.

  Also exported as `sparkdash_llama_model_size_bytes`,
  `_parameters` and `_context_length`, so T2 can correlate in Prometheus rather
  than only on screen.

  **Verified against the production router**, which corrected an assumption in
  the plan:

  | model | state | size | quant |
  |---|---|---|---|
  | cydonia-24b | sleeping | 15.6G | Q5_K - Medium |
  | gemma4-26b | unloaded | — | — |
  | qwen36-35b | sleeping | 23.1G | NVFP4 |

  `meta` is absent for a model llama.cpp has **never loaded** — it reads the
  GGUF header on load, so an unloaded model has no size to report. Sleeping
  models keep theirs. The plan said "a sleeping model still has a size", which
  is true, and implied every state does, which is not. Null renders as `—`
  rather than zero, which is the distinction that matters on a card whose job
  is telling you how big a model is.

  qwen36-35b at NVFP4 and 23.1G is also the first time the dashboard has shown
  why two similarly-named models occupy very different amounts of the pool.

- [x] **T2. Load and unload durations, derived from the state series.**

  `ModelEvent` already carries `ts, node, router, model, from_state, to_state,
  label, cold` and no duration. `extract_events` already walks the one-hot
  series grouped by `(node, router, model)`, so pairing an entry into `loading`
  with the following `loading → active` is local to code that exists.

  Surface via `/api/models/timeline`, which already fetches exactly these
  series — add a per-model summary to its response (last load duration, last
  unload, cold-start count) rather than a second endpoint and a second query.
  The Models card then consumes one endpoint on the cadence `SwapTimeline`
  already uses.

  **Keep it out of the agent.** Durations are historical and the agent is
  deliberately stateless about history; deriving them centrally also means they
  survive an agent restart and reach back as far as retention, which is the
  same reasoning that put the timeline in Prometheus rather than in a table.

  **Shipped 2026-08-19, and the plan needed two corrections.**

  **Correction 1: it IS a second query, unavoidably.** The plan said to add a
  summary to `/api/models/timeline` and avoid a second fetch. The summary is
  there and costs no extra Prometheus round-trip *for a given call* — but
  `SwapTimeline` calls that endpoint at 60–600s steps depending on window, and
  at those steps a real load cannot be resolved at all. The Models card
  therefore makes its own call at `step=15s`. Same endpoint, same code path,
  separate call.

  **Correction 2: unload durations do not exist to be measured.** Freeing
  weights completes inside one scrape, so an unload is a point, not an
  interval. What ships is load duration; the unload timestamp is already in the
  timeline events.

  **The estimator, and why it is `samples × step`.** With samples spaced
  `step`, a model observed `loading` for `m` consecutive samples started
  somewhere in the gap before the first and finished somewhere in the gap after
  the last, so the true duration lies in `[(m-1)·step, (m+1)·step)`. The
  midpoint `m·step` is the point estimate and `step` is the error bar. Counting
  samples and subtracting — the obvious version — measures `(m-1)·step` and
  reports a real 20s load as 0s.

  This stays *correct* at any step and merely loses precision, which is why the
  endpoint does not refuse coarse ones: at 60s a one-sample load reports
  60±60s, and the true 30s is inside that. The card asks for 15s to tighten it.

  **Validated against 24h of real production data**, not just fixtures: 12
  completed load episodes, all successful, 15–30s each. qwen36-35b at 23.1 GiB
  in ~30s is roughly 800 MB/s, which is NVMe-plausible — and is exactly the
  cross-check T1's size column exists to enable.

  Rendered as `~30s` with a leading tilde so it is never read as a measurement,
  `—` when no load happened in the window (not the same as "loaded instantly"),
  and the ± in the tooltip. A failed poll leaves the previous answer up rather
  than blanking the column.

**Order: T1, then T2.** T1 is a live snapshot field with no new request and no
history involved, and it is the column that makes T2's number mean something.

### U — More themes, and making theme validation repeatable

Planned 2026-08-19. Measured cost of adding one: **23 CSS tokens** in a new
`:root[data-theme='x']` block (26 exist, 3 are inherited), plus two lines in
`theme.svelte.ts` — the `THEMES` entry and the `ThemeId` union. Mechanically
trivial.

The cost is not the tokens. `theme.svelte.ts` already records that **two
candidate themes were cut for failing validation**: a green-phosphor look where
green/teal/amber fell below the separation floor even for full colour vision,
and a muted slate that read as grey. Do not re-propose either.

Three findings from checking the current state.

**The validation is not reproducible.** The palette checker that produced those
rejections is not in this repo — it lived in tooling on one machine. Anyone
adding a theme, including a future session of this work, has no way to run the
check the docstring describes, and the two rejections survive only as prose.

**The light theme already carries a contrast WARN.** Ran 2026-08-19 against its
own surface `#fcfcfb`: `--chart-4` `#eda100` at **2.11:1** and `--chart-5`
`#e87ba4` at **2.62:1**, both under the 3:1 floor. That is not dismissable on
its own — it obligates visible labels or a table view. The always-present chart
legend supplies exactly that, so the requirement IS met; but it is met by a
decision made elsewhere for another reason. If the legend ever became optional,
light would silently drop below the floor with nothing to catch it.

Dark and cyberpunk pass every check. Worst adjacent pair in both is
`#c98500 ↔ #199e70` at ΔE 8.4 protan — above the floor, but not by much, which
is worth knowing before anyone "just tweaks" a hue.

**Cyberpunk's eight chart slots are byte-identical to dark's.** Only the
surfaces and chrome differ. That is legitimate and validated, but it means
cyberpunk is a *chrome* theme rather than a data one — so a fourth theme in
that mould is nearly free, while a genuinely new data palette is not.

- [x] **U1. Vendor the palette check into the repo, as a test.** — shipped 2026-08-19 Do this first;
  it is what makes everything after it safe.

  Parse `app.css`, pull each theme's eight `--chart-*` slots and its surface,
  and assert the floors: lightness band, chroma, adjacent-pair CVD separation,
  normal-vision separation, contrast against that theme's own surface. A theme
  that fails then cannot merge, which turns a documented practice into an
  enforced one — the same move as every other "measured, not assumed" thing
  here.

  Encode light's known WARN as an explicit allowance with a comment naming the
  legend as its relief, so the exception is recorded rather than re-discovered.

  **Shipped as `scripts/palette_check.py` plus `tests/test_palettes.py`.**

  Both a report and a gate, deliberately. Designing a theme needs numbers
  (`uv run python scripts/palette_check.py` prints every check for every
  theme); enforcing one needs pass/fail. A gate alone would tell you a palette
  failed without telling you by how much.

  **Themes are discovered by parsing `app.css`, not from a list beside it**, so
  a theme cannot be added to the stylesheet and quietly skipped. Mode comes
  from each block's own `color-scheme` rather than being inferred from surface
  luminance — inferring it would be a second source of truth that could
  disagree with what the browser is actually told.

  **The numbers reproduce the external tool exactly**, which was the point:
  dark's worst adjacent pair at ΔE 8.4, light's at 10.1, normal-vision floors
  19.3 and 19.6, and light's two contrast exceptions. So this agrees with the
  figures already recorded rather than establishing a second, subtly different
  standard.

  Light's WARN is encoded as `CONTRAST_ALLOWANCES` with the legend named as
  what discharges it, and a test asserts allowances only reference themes and
  slots that exist — an exemption outliving its subject would silently apply to
  something it was never argued for.

  **Verified to fail, not just to pass.** Two negative tests reconstruct the
  cut candidates — the green-phosphor shape trips CVD separation *and* the
  normal-vision floor, the muted slate trips the chroma floor. Then both
  enforcement paths were exercised against the real stylesheet: greying
  forest's `--chart-3` fails that theme by name, and adding an unregistered
  `ghost` block fails the drift check. A validation suite that has never failed
  is decoration.

  The drift check is the extra one worth having: a `PaletteId` with no CSS
  renders as `:root`'s defaults with no error, and a CSS block nobody
  registered is unreachable. Both shapes have precedent in this repo.

- [x] **U2. "Auto" — follow the system.** — shipped 2026-08-19, and now the default No new palette at all, and probably
  the most-wanted entry on this list.

  `prefers-color-scheme` appears **nowhere** in the frontend and the default is
  a hardcoded `'dark'`, so a reader whose machine is in light mode gets dark
  until they go and change it. Auto reuses two already-validated palettes and
  needs no colour work.

  One real wrinkle: charts resolve CSS custom properties into literal canvas
  colours when they build, which is why `Theme` applies the attribute
  synchronously in its constructor and `MetricChart` takes a `themeKey`. The
  system preference can change *while the page is open*, so the media-query
  listener has to bump that key the same way an explicit switch does — a theme
  change nobody clicked is exactly the case that would otherwise leave charts
  painted in the old palette.

  **Shipped.** `Theme` now separates the reader's *selection* from the *resolved
  palette*: `auto` is a rule, not a palette, and `data-theme` never carries it —
  writing `auto` to the document would leave it on `:root`'s defaults with no
  way to reach light. `THEMES` gained an entry whose `dark` flag is absent,
  because for `auto` the answer depends on the system and can change while the
  page is open.

  `App.svelte` now passes `theme.resolved` as the chart `themeKey` rather than
  `theme.current`. That is the whole reason the split exists: under `auto` the
  selection never changes while the palette does, so keying charts off the
  selection would have left every canvas painted in the old colours at sunset.

  Behaviour change worth naming: a reader with nothing stored now follows their
  system instead of getting dark. Anyone who has already chosen keeps their
  choice — the stored value is untouched.

  **Not verified: the live system-flip.** The listener is wired and typechecked
  and `auto` resolves correctly on load (confirmed in a dev server against the
  production backend), but flipping the OS appearance mid-session was not
  exercised. That path is one `matchMedia` listener bumping `resolved`.

- [x] **U5. Forest — black surfaces, green accent.** Requested 2026-08-19.

  Built as a CHROME theme in cyberpunk's mould, which is what made it cheap:
  the eight chart slots are the dark theme's validated set, re-run against the
  new surface `#0a0f0a` — all checks pass, worst adjacent pair
  `#c98500 ↔ #199e70` at ΔE 8.4 protan.

  **`app.css` argues against this look and the theme answers the argument
  rather than ignoring it.** The base comment reads: *"Warm-neutral rather than
  pure black — pure black plus one acid accent is the reflexive technical
  dashboard look, and it makes every status colour scream."* Black plus a
  single bright green is precisely that. The resolution: the green cast lives in
  **non-semantic chrome** (rules, tracks, secondary ink), the status ramp keeps
  the dark theme's values, and the data palette is untouched — so nothing
  screams except the one thing that should.

  **The accent green is `--good`, deliberately.** There is no `--accent` token in
  this system, so an accent has to land on a real one. Putting it anywhere else
  would have meant two greens on screen at once — the brand's and the status
  ramp's — which is worse than one green meaning both. `#76b900` measures
  **8.02:1** against `--panel`, past the 4.5 these tokens need as text, and
  better than the dark theme's own `--good` at 5.19.

  A green DATA palette was not attempted: that is exactly what got the
  green-phosphor candidate cut, and ΔE 8.4 leaves no headroom to spend.

  Every text-bearing token was measured against the new surface rather than
  assumed — ink 17.27, ink-2 11.39, ink-muted 7.52, series 5.32/5.11/5.68,
  status 8.02/10.54/7.33/5.13. All clear 4.5.

  **Noted while here:** cyberpunk overrides `--series-1..3` to theme hues while
  leaving `--chart-1..3` at the base values, which breaks `:root`'s stated
  invariant that "the first three ARE the node hues … so a metric colour never
  collides with a node colour by accident". No collision results, because the
  two sets are disjoint there — but the invariant holds by luck rather than by
  construction in that theme. This theme keeps them aligned. Worth resolving
  when U1 lands.

  **Renamed from `nvidia` to `forest` the same day**, at the requester's call:
  the palette is a restrained green cast rather than the bold brand colours the
  name promised, and a theme named for a company should look like the company.
  The hex stays — it is NVIDIA's green, kept on merit rather than branding,
  being both the highest-contrast option tried and a better `--good` than the
  dark theme's own.

  The rename ships with a migration, because it was already deployed when the
  call was made. `RENAMED` maps the old stored id to the new one in `read()`,
  so a reader who had selected it keeps it. Without that, an unknown stored id
  falls through to the default and the reader experiences "my setting was
  forgotten" rather than "that theme has a new name".

- [x] **U3. High contrast.** Shipped 2026-08-21 as `contrast`.

  **Built by search, not by taste.** The requirement was "stepped for maximum
  separation rather than derived by pushing dark's values apart", and the
  measurement behind it holds: the dark set's worst adjacent pair is ΔE 8.4
  against a floor of 8, so there was no headroom to borrow and separation had
  to be constructed.

  **The technique is a lightness ladder.** Protanopia and deuteranopia collapse
  red-green hue difference almost completely; lightness difference survives
  both intact. So eight lightness steps carry the separation and hue is free to
  add rather than obliged to carry it.

  **Interleaved, not monotonic** — the step that made it work. Assigned in
  order, adjacent slots sit one rung apart and most of the benefit is lost.
  Measured: monotonic ΔE 10.2, interleaved **20.6**.

  | | dark | contrast |
  |---|---|---|
  | adjacent CVD ΔE | 8.4 | **20.6** |
  | adjacent normal ΔE | 19.3 | **24.6** |
  | min contrast vs surface | 3.0+ | **4.8:1** |
  | worst text vs panel | 5.1–6.0:1 | **8.4:1** |
  | rule vs panel | ~1.5:1 | **4.1:1** |

  **Hue spacing was constrained to 25°, and it cost separation.** Unconstrained
  the search reached ΔE 25.0; requiring 25° between hues drops it to 20.6 and
  buys a real spectrum instead of four blues. Worth it: with four blues nobody
  can say *which* series they mean out loud, and an accessibility theme that
  defeats verbal description has traded one barrier for another.

  **The band check had to learn an exception, and that is a real cost.** The
  lightness band exists so no slot vanishes or glares, and it doubles as a
  uniformity rule — slots at one lightness carry equal visual weight. This
  theme trades that away deliberately, so `BAND_OVERRIDE` widens it to
  0.55–0.90 for `contrast` alone, with the reasoning recorded next to it. Its
  slots are **not** equally weighted; that is the price of the separation.

  **A first attempt failed the chroma floor, which is why the floor measures
  RENDERED chroma.** The ladder originally ran to L 0.93, and above roughly
  0.85 sRGB cannot hold C 0.12 for warm hues — so the requested chroma clipped
  and the amber slot came back at C 0.076, a grey. The checker measures what
  the colour actually is rather than what was asked for, which is the only
  reason that was visible.

  **Rules are drawn here, not implied.** Everywhere else a border is a hint and
  separation comes from surface lift — precisely what disappears at low vision.
  4.1:1 against 1.5:1 elsewhere, asserted by test rather than left to taste.

- [x] **U4. Chrome-only variants.** Shipped 2026-08-21: `slate` and `paper`.

  Cheap and taste-driven as predicted, with the surface-contrast check re-run
  because that is the one thing a new surface can break on its own. Both pass
  unchanged palettes.

  - **`slate`** — cool blue-grey ground, the dark theme's validated slots.
  - **`paper`** — warm off-white, the light theme's slots. **This one fills a
    real gap rather than a taste one:** there were three dark themes and one
    light, so anyone working in daylight had a choice of exactly one and no way
    to opt out of a cool near-white. Its two contrast allowances (amber and
    pink, discharged by the always-present legend) are recorded against it
    explicitly rather than inherited silently from `light`.

  **The validator gained the check it was missing.** `check()` only ever
  covered the eight chart slots against the surface — nothing validated body
  text, the muted tier or the four status colours, so a theme could pass every
  palette check and still render unreadable prose, which is most of the page.
  `tests/test_palettes.py` now holds all seven text tokens to 4.5:1 against the
  panel they are actually drawn on, across every theme. The status ramp counts
  as text because it *is* text here — health reasons and runtime names are
  printed in those colours, not merely dotted with them.

**Order followed: U1, U2, then U3/U4.** U1 made the rest checkable — and paid
for itself twice over, catching the clipped-chroma grey in U3's first palette
and the missing chrome check that U4 added.

### V — More inference runtimes: SGLang, and Atlas — **shipped 2026-08-21** (V2b deferred)

Planned 2026-08-19. Then the agent collected from exactly two engines,
`COLLECTIBLE_RUNTIMES = {"llama.cpp", "vllm"}`. Both of the engines below are
launched by [Sparkrun](https://forums.developer.nvidia.com/t/sparkrun-central-command-with-tab-completion-for-launching-inference-on-spark-clusters/360832),
which runs vLLM, SGLang and llama.cpp solo or clustered on Spark boxes.

**Start from what already works, because it is more than it looks.**
`_runtime_for` in `collectors/gpu.py` already recognises SGLang
(`if "sglang" in haystack`), and `LLM_RUNTIMES` already contains it. So an
SGLang server running on a node right now is already classified as an LLM
runtime, already attributed to the `models` class of the memory band rather
than `other gpu`, already labelled in the GPU process table — and already
raises `UnmonitoredInferenceRuntime`, telling you it is serving with nothing
collecting from it. The detection exists. Only the collector is missing.

**Correction, found while doing V1: it did NOT raise the warning.**
`COLLECTIBLE_RUNTIMES` was `{"llama.cpp", "vllm"}`, and the gap detector
deliberately reports only runtimes there is a collector *for* — a warning that
cannot be resolved teaches the reader to ignore the indicator. So SGLang was
classified and attributed correctly and was **silent**, which is the failure
this area exists to catch, one level up. Adding it to `COLLECTIBLE_RUNTIMES` is
part of V1 rather than something already in place. The same now applies to
Atlas, which V2a classifies but deliberately leaves out of that set.

- [x] **V1. SGLang collector.** Shipped 2026-08-21.

  Verified against SGLang's own docs 2026-08-19: `/metrics`, enabled with
  `--enable-metrics`, examples on port 30000, metric names prefixed `sglang:`.
  That is the same shape `VllmCollector` already handles — scrape a text
  exposition endpoint, parse a handful of gauges — so the collector is close to
  a sibling of one that exists rather than new ground.

  Mapping, as far as it goes cleanly:

  | SGLang | maps to |
  |---|---|
  | `sglang:num_running_reqs` | `requests_running` |
  | `sglang:num_queue_reqs` | `requests_waiting` |
  | `sglang:gen_throughput` | `tokens_per_sec` |

  **The trap: `sglang:cache_hit_rate` is NOT vLLM's `kv_cache_usage_ratio`.**
  One is the fraction of prompt tokens served from the prefix cache; the other
  is how full the KV cache is. They are different questions with the same
  shape, and putting the first under the existing `kv` column would render a
  number that looks like occupancy and is not. Either give it its own column or
  leave `kv` empty for SGLang rows — an empty cell is honest, a wrong one is
  not.

  **Settled: `kv` is left empty**, and `cache_hit_rate` is not collected at
  all. A column for one engine's number is a column that is blank on every
  other row, and the question it answers is not one anyone has asked yet.
  `sglang:token_usage` is the closer analogue to occupancy and is the candidate
  if the column is ever wanted — noted in `SPECS`, not implemented, because it
  has not been checked against a running server.

  **A second trap of the same kind, found while implementing:
  `sglang:gen_throughput` is not the tokens/sec this dashboard means.** It is
  instantaneous *decode* throughput over the engine's last batch, while vLLM
  and the llama.cpp routers contribute prompt+generation counted over the poll
  interval — and the node card SUMS those into one figure. Adding the two would
  produce a total that is neither. So throughput is derived from
  `sglang:prompt_tokens_total` / `sglang:generation_tokens_total` like every
  other engine's, and the gauge is used only when the counters are missing from
  a scrape.

  **Also fixed, because V1 walked straight into it:** the `tokens_per_second`
  history query was `sum by (node) (llama) + sum by (node) (vllm)`. Binary `+`
  between instant vectors keeps only label sets present on BOTH sides, so that
  returned **nothing** for a node running only one of them. Every node here
  runs llama.cpp, so it read as correct; a vLLM-only node was charting a flat
  blank while serving tokens, and a third engine would have made it worse. Now
  one `sum by (node)` over a `__name__` regex — no matching step, and an engine
  added later joins by name.

- [x] **V2a. Classify Atlas.** Shipped 2026-08-21, ahead of the rest.

  [Atlas](https://atlasinference.io/) is an open-source LLM engine in pure Rust
  and CUDA, hand-tuned for DGX Spark, shipping as a single ~75 MB binary with
  no Python or PyTorch, serving multiple models from one process and launched
  through `sparkrun`.

  **The agent does not recognise it at all** — no marker in `_runtime_for`, and
  absent from `LLM_RUNTIMES`. The consequence is not merely a blank label: an
  Atlas process is counted as **other GPU** in the memory band rather than
  `models`, so on a node running Atlas the one chart that answers "what is
  eating the pool" attributes every byte of it to the wrong class. That is
  wrong rather than incomplete, and it is a two-line fix independent of
  everything else here.

  Needs a marker that will not misfire: "atlas" is a common enough word that
  matching it bare in a full argv+cwd haystack risks the mislabelling
  `_looks_like_comfyui` exists to avoid. Prefer the binary name.

  **Done that way.** `_looks_like_atlas` matches the process name and argv[0]'s
  basename only — never the haystack — so `--dataset atlas-corpus`, a model
  named `org/atlas-7b` and `-m /models/atlas.gguf` are all left alone, each
  pinned by a test. Atlas is in `LLM_RUNTIMES` (so its memory lands in
  `models`) and deliberately NOT in `COLLECTIBLE_RUNTIMES`: with nothing known
  to scrape, flagging it would raise a warning nobody can resolve.

- [ ] **V2b. Atlas collector — DEFERRED 2026-08-21 on install base, not on
  capability.**

  It was written as blocked on a technical question: Atlas documents no
  Prometheus endpoint and does not say whether its API is OpenAI-compatible, so
  the first step was to run it and look. That question is still unanswered and
  is no longer the reason to wait.

  **The GB10 community runs llama.cpp, vLLM and SGLang** — all three of which
  are collected. Atlas is young and its install base is unclear, so building a
  collector for it means maintaining a code path that may have no users, in a
  place where every engine added multiplies the surface `V3` was written to
  contain.

  **The valuable half already shipped.** V2a classifies Atlas, so on a node
  running it the memory lands in the `models` class rather than `other gpu` —
  which was the part that was *wrong* rather than merely missing. It is
  deliberately outside `COLLECTIBLE_RUNTIMES`, so it raises no warning nobody
  can resolve. A node running Atlas today is accounted for; it just has no
  throughput or queue depth.

  **The trigger to revisit is adoption, not curiosity.** If Atlas turns up on a
  node here, or the community moves to it, the work is small — V3 made adding
  an engine a `SPECS` entry, a `Runtimes` field, a scrape job and two regex
  updates the wiring test names for you. Until then this is a collector for a
  runtime nobody is running.

- [x] **V3. Decided 2026-08-21: share the collector, keep the wire.**

  Measured 2026-08-19: `vllm` is named in **27 tracked files**, `llama_cpp` in
  12. `Runtimes` has one named list field per engine; the exporter builds a
  metric family per engine; the frontend's types, tables and memory band all
  name them. A third and fourth engine multiply that.

  This is the counter-case to [U/§modularity](#u--more-themes-and-making-theme-validation-repeatable)'s
  conclusion about cards. Cards are heterogeneous — each is a different table
  meaning a different thing — so hand-wiring them is honest. Runtimes are
  genuinely homogeneous: every one is "scrape an endpoint, get models,
  requests, throughput". That is the shape abstraction actually pays for, and
  V1 is the moment to decide, because doing it after four engines is a
  refactor and doing it during the second is speculation.

  **Decided: share the collector, keep the wire.** `EngineCollector` takes an
  `EngineSpec` — a table of what one engine calls the things every engine
  reports — so vLLM and SGLang scrape, parse and rate-track through one
  implementation. What is NOT abstracted is the wire: `Runtimes` keeps a field
  per engine and the exporter keeps emitting `sparkdash_vllm_*` and
  `sparkdash_sglang_*` rather than one family with a `runtime` label.

  **Why stop there.** The label would be the tidier design and buys nothing
  here: the vLLM names are what `alerts.yml`, the history queries and every
  recorded series are already written against, so unifying them is a migration
  of stored data in exchange for symmetry. Per-engine names are also what lets
  a scrape job, a target file and the retire button all be named for one
  engine — `job="sglang"` is a label an operator can read at 2am.

  **The line that holds the pieces together is one list**, `ENGINE_RUNTIMES`,
  derived from `Runtimes`'s own fields. The agent builds a collector per entry;
  the backend parses, renders targets for, serves and retires per entry; the
  frontend walks the same list. `tests/test_engine_wiring.py` checks the four
  copies that cannot be derived from each other — Python, TypeScript,
  `prometheus.yml`'s scrape jobs, and the two alert rules that must stay
  complements — because each way of drifting is silent.

  **Measured after:** adding SGLang touched no per-engine `if` in the agent's
  collect path, and adding engine #4 is a `SPECS` entry, a `Runtimes` field, a
  scrape job, and two regex updates the wiring test names for you.

**Sparkrun itself is worth a look while doing V1**, though not an item yet: if
these nodes end up launched through it, "what is running here" becomes
something it knows and the agent currently infers from process argv.

**Effectively complete after 2026-08-21.** V1, V2a and V3 shipped; V2b is
deferred on install base rather than blocked on a question. The engines the
GB10 community actually runs — llama.cpp, vLLM, SGLang — are all collected, and
Atlas is classified so its memory is attributed correctly even though nothing
scrapes it.

### W — Choosing which interfaces are monitored — **shipped 2026-08-21**

Planned 2026-08-21, from a live failure rather than an audit. The second
ConnectX-7 port on each node was cabled to the 100Gb switch as a test, then
unplugged — the extra links were interfering with standing up clustered vLLM.
Cluster operation was unaffected: the direct spark-to-spark link carries the
fabric, and the 10Gb port is more than enough for management and client access.
The dashboard has been alerting on the unplugged ports ever since, returning at
most every 24 hours.

**What is actually alerting**, read off the live agents 2026-08-21:

| node | interfaces | down and previously up |
|---|---|---|
| `sparky` (.61) | `enP7s7` 10Gb up, `wlP9s9` wifi down | none — wifi was never up |
| `sparketa` (.62) | two 200Gb pairs + 10Gb + wifi | `enP2p1s0f1np1`, `enp1s0f1np1` |
| `sparkjr` (.63) | same | `enP2p1s0f1np1`, `enp1s0f1np1` |

So it is **two** down ports per new node, not one — each new node has two
dual-port 200Gb cards, with both `f0` ports up at `200 Gb/sec (2X NDR)` and
both `f1` ports pulled. And each `f1` port has a RoCE device behind it
(`roceP2p1s0f1`, `rocep1s0f1`) reporting DOWN after previously being ACTIVE, so
`RdmaPortDown` is firing alongside `NetworkLinkDown`: **eight** firing series,
not four. That is why W3 covers RDMA rather than leaving it for later — half
the noise is on that side.

**Why [A4](#a--alerting-correctness)'s heuristic cannot fix this, and was never
going to.** `NetworkLinkDown` fires on `sparkdash_network_up == 0 and
max_over_time(sparkdash_network_up[7d]) == 1` — "was up at some point this
week" — which separates a link that *failed* from one that was never in
service. That is the right question for a never-cabled port and the wrong one
here: these ports **were** in service, so they read as links that failed. The
heuristic infers intent from history, and there is no history that distinguishes
"I unplugged this" from "this died".

**The 24-hour period is the silence cap, and it is pointing at the fix.**
Silences created from the dashboard are capped at `MAX_SILENCE_HOURS = 24`, and
that cap's own reasoning says why: "a mute that outlives the person's memory of
setting it is how a real failure goes unnoticed, and a permanently unwanted
alert should have its target removed from configuration instead." The system
has been saying *this belongs in configuration* once a day. The configuration
does not exist for interfaces. That is the whole of this section.

**It would also stop on its own after seven days — and that is worse, not
better.** Once the ports have been down longer than the window, `max_over_time`
stops matching and the alert resolves itself while the links are still down. So
the same rule that nags today goes quiet about a genuinely dead link on the
eighth day. Both halves of that are the absence of a way to say what is
intended.

- [x] **W1. Read admin-down and no-carrier — VERIFIED AND ABANDONED 2026-08-21.**
  The premise was wrong, and the measurement is worth keeping.

  The idea was that `operstate` says "down" for two different situations, and
  that `flags` (bit `0x1`, `IFF_UP`) plus `carrier` would separate a deliberate
  `ip link set X down` from a lost cable — giving the dashboard the intent the
  7-day heuristic was inferring from history.

  Measured on `sparketa` and `sparkjr`:

  | interface | `operstate` | `flags` | `carrier` |
  |---|---|---|---|
  | `enP2p1s0f0np0` — cluster fabric, up | up | `0x1003` | 1 |
  | `enP2p1s0f1np1` — **cable pulled** | down | `0x1003` | 0 |
  | `wlP9s9` — wifi, never cabled | down | `0x1003` | 0 |

  `0x1003` is `IFF_UP|IFF_BROADCAST|IFF_MULTICAST`: **every** interface is
  administratively up, including the ones nobody has ever used. Nothing on
  these nodes is admin-down, so the distinction exists in the kernel and is
  constant in practice — `carrier` is `operstate` restated, and the pulled
  cable is indistinguishable from the never-cabled wifi by exactly the signal
  that was supposed to tell them apart.

  It could be *made* to work by running `ip link set <iface> down` on each
  node, which is the rejected alternative below wearing a different hat: it
  moves the decision onto three hosts and leaves no record in the dashboard.
  W2 answers the same question better, so the collector is left alone rather
  than gaining a field whose value is `true` on every row.

  **What this changes downstream:** W1 was going to let the panel say `down (no
  carrier)` against `down (admin)`. It cannot, so a down interface says only
  that it is down, and *why* stays a question for the operator. It also means
  W1 removes nothing from the ignore list — the full set below is real work,
  not a fallback.

- [x] **W2. A per-node ignore list in `cluster.yml`, served to the agent.**

  ```yaml
  - id: sparky
    host: 192.168.50.61
    interfaces:
      ignore:
        - enP2p1s0f1np1   # switch port, cable pulled 2026-08-21
  ```

  **Default monitored, named to exclude** — not an allowlist. It keeps A4's
  best property, that a newly cabled port is watched the moment it comes up
  with nothing to remember, and it fails in the safe direction: forgetting to
  maintain the list makes the dashboard noisy, never silent. On a system whose
  recurring failure mode is silence, that asymmetry decides it.

  Travels the path the runtimes already travel — `cluster.yml` →
  `/api/agent-config` → `RemoteConfig` → the agent — so this is one more key,
  not a new mechanism.

  **A trap created by [V](#v--more-inference-runtimes-sglang-and-atlas--shipped-2026-08-21-v2b-deferred):**
  `RemoteConfig` now treats *every* list-valued key under `runtimes:` as an
  engine's endpoint list, deliberately, so a node picks up an engine a newer
  backend knows about. `interfaces` must therefore be a **sibling of
  `runtimes`**, never a key inside it — nested, an ignore list would be parsed
  as an engine named "interfaces". Harmless at runtime (there is no spec for
  it, so nothing scrapes it) and silently wrong, which is worse.

  **No environment fallback, deliberately.** A node not in `cluster.yml` keeps
  today's behaviour — everything monitored — which is the correct default
  anyway. That adds no per-node variable and keeps the node stack identical
  across the cluster, which is the property `.env` parity exists to protect.

  **No cluster-wide defaults block in v1.** Three identical nodes will carry
  three identical lists, which looks like something to factor out — but W4's
  editor writes them, so the duplication costs nothing to maintain, while a
  defaults-plus-override merge rule is a thing to explain forever. Revisit if
  the cluster grows past a handful of nodes.

- [x] **W3. One metric, four alert rules.**

  `sparkdash_network_monitored{node,interface}`, 0 or 1, emitted for every
  interface the agent sees.

  **Not a label on `sparkdash_network_up`.** Adding a label to an existing
  series splits its history at the deploy — the same reason a node id is chosen
  once and never changed.

  **Not achieved by dropping unmonitored interfaces from the export.** Their
  history would vanish and the panel would lose interfaces that physically
  exist. Report and mark; never omit. That rule is why an unreachable engine
  endpoint is reported rather than dropped, and it holds here for the same
  reason.

  `NetworkLinkDown` and `NetworkErrorsRising` gain
  `and on (node, interface) sparkdash_network_monitored == 1`. **The 7-day
  guard stays** — it still does the never-cabled work for any deployment that
  has configured nothing, and the two are complementary rather than redundant.

  **RDMA has to follow, or one pulled cable trades two alerts for two others.**
  The f1 port is also a RoCE port, so `RdmaPortDown` and `RdmaErrorsRising`
  would carry on alone. `sparkdash_rdma_port_monitored{node,device,port}`,
  derived from the paired netdev rather than configured separately —
  `RdmaPort.interface` already carries that pairing, which exists because mlx5
  leaves the InfiniBand byte counters at zero on an Ethernet link layer. A port
  with no netdev pairing defaults to monitored.

  Cardinality is six interfaces across three nodes. Nothing.

  **`unless ... == 0`, not `and ... == 1` — decided by measurement.** The
  obvious form gates the rule on the flag being 1, and it fails in the wrong
  direction: the flag does not exist until every node runs an agent that
  exports it, so a stale node, or a rules reload landing before the rollout
  finishes, would take link alerting **silent** rather than merely unfiltered.
  Checked against the live Prometheus before committing to it: the `and == 1`
  form returned **0** series where `unless == 0` returned the **4** that were
  genuinely down. Set subtraction removes only what is explicitly marked
  excluded, so an absent flag leaves the old behaviour exactly as it was —
  the same "noisy beats silent" asymmetry that decided the ignore list.

  Both forms were also run against live Prometheus to confirm they parse and
  match the expected series, since nothing in CI evaluates PromQL.

  **Naming collision worth noticing:** `unmonitored_runtimes` already means
  something adjacent and different — an inference runtime nothing is configured
  to *collect from*, which is a gap to fix. An unmonitored interface is a
  deliberate exclusion. Same word, opposite intent, and the docs should not let
  them blur.

- [x] **W4. The status element, and choosing from the UI.**

  **Colour answers "is it up"; a tag answers "do we watch it".**

  | state | reads as |
  |---|---|
  | up | green |
  | down, monitored | red — a fault |
  | down, not monitored | grey, tagged `not monitored` |
  | up, not monitored | green, tagged `not monitored` |

  **This fixes an inversion that exists today independent of the config work.**
  A down link currently renders in muted ink — visually identical to an unused
  port — so the most alarming row in the table is also its quietest. With W1 the
  down state can also say which kind it is: `down (no carrier)` against
  `down (admin)`.

  **The settings fly-out gets checkboxes, not a text field:** each node's
  interfaces as the live snapshot reports them, ticked when monitored. Nothing
  is typed, which is narrower than free text and the same instinct as
  ports-rather-than-URLs in the cluster editor.

  **A name in the config that the node is not currently reporting must survive
  a save.** A node that is down, or a NIC that was renamed, would otherwise
  have its entry silently deleted by an editor that only knows what it can
  currently see — a save while a node is unreachable would quietly erase its
  configuration. Render those as read-only rows and write them back untouched.

  **Node health is unchanged.** A pulled cable does not repaint the cluster
  view; `health` stays GPU, memory, PSI and temperature. The alert and the panel
  carry it, which is the whole point of having both.

- [x] **W5. Roll it out, then clear the silences.** Operational, not code.
  **Done, confirmed 2026-08-28:** all three agents are on the current build and
  Alertmanager reports zero active silences. Nothing left to clear.

  **Order does not matter for correctness** — that is what the `unless` form
  bought — but it does decide when the noise stops:

  1. Build and push the agent image on **one** node (all three are arm64, and
     two nodes pushing the same tag would leave different digests behind one
     tag), then redeploy the node stacks. Every node is on `b074d8e` today,
     several commits behind, so this also ships [V](#v--more-inference-runtimes-sglang-and-atlas--shipped-2026-08-21-v2b-deferred).
  2. Redeploy central for the new rules, backend and dashboard.
  3. Tick the boxes off in settings — `enP2p1s0f1np1` and `enp1s0f1np1` on
     `sparketa` and `sparkjr`. Nothing to hand-edit.
  4. Drop any standing 24-hour silences rather than letting them expire, so the
     alert list reflects configuration rather than a mute that is still
     running.

  Two unrelated alerts are also firing and are worth a look while there:
  `TemperatureBandsNotDerived` on `sparkjr` — it is judging temperature against
  fallback guesses rather than its own silicon, which is what
  [A3](#a--alerting-correctness) exists to catch — and `MemoryNearlyFull` on
  both new nodes.

**Considered and rejected:**

- **A regex in `alerts.yml`** — `sparkdash_network_up{interface!~".*f1np1"}`.
  This genuinely is the five-minute fix and remains a reasonable stopgap for
  today. Rejected as the answer: it makes an operational decision a repo edit
  plus a Prometheus reload, it cannot differ per node without growing the
  regex, and it leaves the dashboard unable to say anything about intent — the
  panel would still show a bare down link with nothing indicating anyone meant
  it.
- **`ip link set <iface> down` on each host.** The honest Unix answer, and W1
  makes the dashboard read it correctly rather than guessing. Rejected as the
  *whole* answer because it moves the decision onto three hosts with no record
  in the dashboard, and it cannot express "this link is cabled and up, and I do
  not want to be paged about it".
- **Roles instead of a boolean** — `cluster` / `management` / `unused` per
  interface, with alerting derived from the role. Richer, and nothing asks for
  it yet. The per-interface map form is the migration path if it ever does.

**Sequence: W1, then W2, then W3 and W4 together.** W1 stands alone and improves
the panel whether or not the rest lands; W3 has nothing to join against without
W2; W4's editor cannot write a shape W2 has not defined.

### X — Grafana as a first-class consumer

Planned and built 2026-08-21, from a question worth answering in the repo: if
someone would rather build their own views than use the bundled frontend, is
that easy? It nearly was. Prometheus already holds everything, is published
without auth on `:9090`, and keeps 180 days — but a newcomer had no way to know
what any of the 73 `sparkdash_*` series meant, because
[docs/metrics.md](metrics.md) documents what vLLM and llama.cpp expose
UPSTREAM, not what this agent emits.

**The surface, measured 2026-08-21:** 896 metric names in the TSDB — 73
`sparkdash_*`, ~314 `node_*`, ~106 `vllm:`/`sglang:`. Worth stating plainly:
because the engines are scraped DIRECTLY rather than proxied through the agent,
a Grafana user has strictly more to work with than the frontend shows.
`vllm:time_to_first_token_seconds`, `vllm:e2e_request_latency_seconds` and the
per-request success/failure counters are all already stored and have never been
rendered anywhere.

- [x] **X1. A starter dashboard**, `central/grafana/spark-dash-overview.json`.
  29 panels over six sections, `$node` templated, importable into any Grafana:
  the datasource is declared as an `__inputs` entry rather than a hardcoded
  uid, so it lands without editing.

  **Every panel carries a description** saying what it means and where it
  misleads. That is doing double duty on purpose — until X2 exists, the
  dashboard IS the catalog for this metric surface, and a description that only
  restates the title would be wasted space.

  **All 32 queries were run against the live Prometheus before shipping.**
  Grafana's transformations were not, because that needs Grafana itself; the
  README says so rather than implying the whole file was exercised.

- [x] **X2. The traps, written down** in `central/grafana/README.md`. These are
  not general PromQL advice — each one is specific to this deployment and would
  otherwise be found the hard way:

  - **Never sum a memory pool across nodes.** GB10 has no separate VRAM, so
    three nodes' pools added together describe a single 384 GB space nobody can
    allocate from. The pool panel repeats per node rather than aggregating,
    which is the same reasoning that keeps `cluster` off standalone nodes.
  - **`sum(A) + sum(B)` is not one sum over two families.** Binary `+` keeps
    only label sets present on BOTH sides, so a node running one engine
    contributes nothing and charts flat zero while it serves. This was a live
    bug in `HISTORY_QUERIES`, found during [V](#v--more-inference-runtimes-sglang-and-atlas--shipped-2026-08-21-v2b-deferred)
    and fixed there; the dashboard would have reproduced it exactly.
  - **Some `_total` series are typed as gauges.** The network byte counters are
    monotonic sysfs counters exported through a gauge family, so Grafana will
    not suggest `rate()` and a linter may object. `rate()` is correct on them.
  - **llama.cpp throughput is a pre-computed gauge**, not a counter, so it
    cannot be re-rated over a window of the reader's choosing. vLLM and SGLang
    can, because their native counters are scraped directly.
  - **States are one series per state**, not an encoded enum — filter on the
    label, there is nothing to decode.
  - **Thresholds are metrics.** `sparkdash_gpu_temp_{warning,critical}_celsius`
    are derived per node from NVML's own slowdown threshold, so a panel draws
    its bands from the silicon rather than hardcoding 82/86.

  **One claim in the first draft was wrong and was corrected by measurement:**
  that `gpu_process_memory_bytes` emits both a per-runtime total and per-model
  rows, so `sum by (node)` double-counts. It does not. The aggregation key is
  `(runtime, model, server)`, so the partition is disjoint — hand-summed
  against `sum by (node)` on a live node to confirm. An empty `model` is a
  router parent holding its own overhead, not a subtotal.

- [x] **X3. The catalogue exists.** Shipped 2026-08-21 in
  [docs/metrics.md](metrics.md).

  **85 metric names**, grouped by area, each with its labels and what it means
  — plus the three that will catch a reader out, cross-referenced to
  `central/grafana/README.md` rather than duplicated there.

  **`tests/test_metrics_catalog.py` checks it BOTH ways**, which is the part
  that makes it worth having. A metric added without a doc entry is
  undocumented; a doc entry with no metric is a name someone will query and get
  nothing back from, which is indistinguishable from a quiet cluster. Both
  failure modes were confirmed to fail the test before shipping it.

  Names come from **rendering a snapshot**, not from grepping the exporter:
  they are built there by three different patterns — `_g("name", ...)`, lists
  of `(name, doc, value)` tuples, and f-strings per engine — so a regex over
  the source silently misses whichever one it was not written for.

  Writing it found two shorthands in my own first draft (`llama_models_known /
  _active / _sleeping`, and a nested `{receive,transmit}_{errors,dropped}`)
  that read fine and expand to nothing. Spelled out rather than teaching the
  parser to guess.

- [ ] **X4. Decide whether Grafana gets a container here.** Deliberately not
  done. `central/compose.yaml` has a hand-maintained deploy copy that drifts by
  design, so a new service is two edits in two repos, and Grafana brings its
  own state directory to back up. Pointing an existing Grafana at `:9090` costs
  nothing and is what the README documents. Revisit only if the frontend stops
  being the primary view.

**Not planned: replacing the frontend with Grafana.** Two things it cannot do.
Per-process GPU detail with pids is aggregated away before it reaches
Prometheus — a deliberate cardinality trade (see [B](#b--per-workload-gpu-memory-history))
— and lives only in the agent's live snapshot. And the frontend polls agents
directly for sub-2s liveness, where Prometheus scrapes at 15s. Grafana is the
better tool for history and for questions nobody anticipated; the frontend is
the better tool for what is happening right now.

### Y — Straggler detection in a pooled cluster (was E6)

Planned 2026-08-21, taking up [E6](#e--more-signal-and-correlating-it) now that
`danflashes` is real and serving. **E6's premise did not survive contact with
the cluster**, and the reframing is most of the work.

**E6 assumed: same model, three nodes, one slower.** The deployment is not that
shape. `sparky` is standalone on llama.cpp; `sparketa` and `sparkjr` are one
cluster running a **single distributed vLLM model**, `deepseek-v4-flash-0731`,
96.8 GiB resident on each of them. They are not three peers serving the same
thing — they are two halves of one thing.

**The consequence, measured: throughput exists on the head node only.**
`sparkjr` has no vLLM series at all — no endpoint, because a tensor-parallel
worker does not serve an API. Comparing tokens/sec per node, which is what E6
proposed, is not merely hard here; there is nothing to compare. The 5 series
that exist on `sparketa` and not `sparkjr` are exactly the engine ones.

So the question changes from *which node is slower* to **is one node holding the
other back** — and it has to be answered from the signals that do exist per
node: clock, temperature, power, PSI, RDMA.

**Why SM utilisation is NOT one of those signals.** The obvious approach is to
compare `gpu_process_sm_percent` and flag the low node. It does not work for
tensor-parallel inference: NCCL collectives **busy-wait**, so a node stalled
waiting on a straggling peer burns SM identically to one doing useful work.
Measured over 90 minutes on `danflashes`, both nodes peak at 96% and track each
other closely. SM says the cluster is busy; it cannot say who is late.

- [x] **Y1. Make "tokens per second" mean one thing.** Shipped 2026-08-21.

  Found while looking for a comparison baseline. `sparkdash_vllm_tokens_per_second`
  is the agent's own rate over its ~1s poll of `prompt_tokens_total +
  generation_tokens_total`, so it mixes **prefill** and **decode** — two rates
  that differ by three orders of magnitude. Measured over 3 hours of real
  serving:

  | | |
  |---|---|
  | reported `tokens_per_second`, non-zero samples | 28, 31, 33, …, 79, then **6583, 7046, 8565, 10603, 14837, 17994, 47672** |
  | `rate(vllm:generation_tokens_total[5m])` | mean 6.2/s, **max 47.9/s** |
  | `rate(vllm:prompt_tokens_total[5m])` | mean 619/s, max 3375/s |

  The dashboard's headline Throughput stat can therefore read **47,672 tok/s
  while the model is generating 48 tok/s**. Both numbers are arithmetically
  correct; only one is what anyone means by throughput. The spikes are a large
  prompt landing inside a one-second window, which is prefill, and prefill is
  not a rate anyone is trying to read off a stat panel.

  Split them: report generation and prompt rates as separate series and let the
  headline be generation. **Do not redefine the existing series in place** — it
  is what recorded history, the history queries and the Grafana starter are
  written against, and silently changing its meaning is worse than adding to it.

  **Verified, and the answer was the convenient one:** llama.cpp does exactly
  the same thing — `tokens_predicted_total + prompt_tokens_total` — so both
  engines conflated identically, the cluster-wide sum was at least consistent,
  and one treatment fixes both.

  **What shipped.** `generation_tokens_per_sec` and `prompt_tokens_per_sec` on
  `EngineMetrics` and `RouterModel`, exported as
  `sparkdash_{engine}_generation_tokens_per_second` and
  `..._prompt_tokens_per_second`. The combined `_tokens_per_second` series is
  **kept and still emitted** — recorded history, the history chip and the
  Grafana starter are written against it, and renaming it in place would orphan
  every stored sample for no gain.

  Everything that leads with a number now reads decode: the header stat, the
  node cards, `/api/cluster/summary`, the Models table's `tok/s` column and the
  Throughput history chip. Prefill gained its own column and its own chip
  rather than being dropped, because a signal collected and never shown is the
  thing [S](#s--three-signals-already-collected-and-not-shown) exists to
  prevent.

  **Y1a. Say which rate the headline is.** Shipped 2026-08-21.

  Y1 fixed the arithmetic and left the caption alone. The headline read
  `tokens/sec` — the same words it used when it meant prefill + decode — so
  nothing on the page distinguished the corrected number from the one it
  replaced. Every other surface had already moved to the convention (`tok/s`
  means decode, `prefill` is its own column); the most prominent number on the
  page was the last one still using the vague name.

  The caption is now `decode tok/s`, and prefill joins the summary row.

  **As a STATE, not a rate, and that is the whole design.** Measured over six
  hours on `danflashes`: prefill is non-zero **1% of the time** and peaks at
  **110,571 tok/s**. A live number there would read `0` almost always, then
  briefly render six digits beside a two-digit decode rate — Y1's exact
  misreading, reintroduced in a smaller font. So it reads `idle`, or
  `ingesting 110k` while a prompt is landing, and the magnitude is all it
  claims to give.

  Placed last in the flex row because `ingesting 110k` is wider than `idle` and
  at the end of the row a widening value has nothing after it to push.

  Guarded three ways, each verified against the mutation it describes: the
  headline must sum `generation_tokens_per_sec` and never the combined field
  (which still exists, still ships, and is still the obvious name to reach
  for); prefill must never be added into the headline sum; and it must render
  through `compact()` with a resting state rather than as a bare rate.

**A small alignment worth noting:** SGLang's `gen_throughput` gauge — the
  fallback when counters are missing from a scrape — is decode throughput by
  definition, so it now lands in the generation field rather than in a combined
  total it never matched.

- [x] **Y2/Y3. Sustained divergence, directional by construction.** Shipped
  2026-08-21 as the `cluster` alert group: `ClusterNodeClockLagging` and
  `ClusterNodeRunningHot`.

  **The naive rule is unusable and the data says so.** Instantaneous spread
  across a *healthy* pair, against the same pair averaged over 15 minutes:

  | metric | instantaneous | 15-minute average |
  |---|---|---|
  | GPU clock | −117 … +110 MHz | **−4.7 … +4.7 MHz** |
  | GPU temperature | −13 … +15 °C | **−1.2 … +1.2 °C** |
  | GPU power | −34 … +56 W | **−1.6 … +1.6 W** |

  A threshold loose enough to survive the left column would miss any real
  straggler. Averaged, the band is ~25× tighter, and 50 MHz / 5 °C sit about
  10× above it.

  **Compared against the best peer, not the cluster mean** — a correction made
  while building. With two nodes the mean sits exactly between them, so each
  deviates by *half* the real gap and a 50 MHz threshold would silently mean
  100 MHz. Against `max` (clock) or `min` (temperature) the number is the full
  gap at any cluster size.

  **Directional, because n=2 cannot vote** (Y3). Two nodes disagreeing says the
  pair differs, not which one is wrong; statistical outlier detection needs
  n≥3. What rescues it is that each metric's own semantics name the bad side: a
  lower clock *is* the node setting the pace, a higher temperature *is* the one
  about to throttle. **Power is deliberately absent** — lower can mean stalled
  or merely idle, higher can mean working hard or leaking. Worth charting, not
  worth alerting on.

  **The clock rule is gated on the cluster working; the thermal one is not.**
  At idle clocks drop independently and the comparison is meaningless — only
  ~15% of a measured 48h window had the cluster busy at all. Temperature is
  different: a node hotter than its peers *at idle* has a cooling problem that
  will only be worse under load, and that is worth knowing before the next run
  rather than during it.

  `ClusterNodeRunningHot` is deliberately distinct from `GpuTemperatureHigh`.
  That one asks whether a node is near its own limit; this asks whether it is
  out of step with peers doing identical work, and fires long before the
  absolute threshold does.

  **THRESHOLDS ARE PROVISIONAL and the rule says so.** Roughly four hours of
  genuinely *paired* data — `sparkjr` joined recently, and for most of the
  longer window the "cluster" was `sparketa` compared against itself, which
  makes every deviation trivially zero. Revisit after a week of two-node
  history.

  Both rules were run against the live Prometheus: they parse, and they are
  quiet on the healthy pair.

  **Not added: a cluster-specific throttle rule.** `GpuThrottled` already fires
  per node, and in a pooled cluster that is the same information arriving by a
  shorter route. A second rule would double-alert on one event.

- [x] **Y4. Surfaced on the node cards.** Shipped 2026-08-21.

  A short badge beside the status pill — `clock lagging`, `running hot` — in
  warning ink, on the cards that are already grouped by cluster.

  **Read from the alert feed, not recomputed.** A card cannot see its peers, so
  it could not derive this alone; and even given the data, the rules compare
  15-minute *averages* while a card would have instantaneous values. Two
  answers that disagree constantly leave a reader unable to tell which to
  believe, so the alert is the single source and the card reflects it.

  **Node health is untouched**, deliberately. A node clocking 60 MHz below its
  partner is not unhealthy on its own terms, and colouring the card for it
  would repeat the mistake [W](#w--choosing-which-interfaces-are-monitored--shipped-2026-08-21)
  avoided: an indicator firing on something nobody can act on. The badge is a
  note *on* the card, not a change of its state — and it sits outside the
  status pill so the two cannot be confused.

  Kept in compact mode, unlike the runtime summary, because "which node is
  dragging" is scan material rather than something you read after deciding to
  look closer.

**What this cannot see, stated so it is not rediscovered.** If the interconnect
itself is the bottleneck, every node looks equally busy and equally warm, and
none of the above fires. The RDMA counters catch *errors*, not *saturation*, and
memory bandwidth is unreachable on GB10 at all — the closed question at the top
of [E](#e--more-signal-and-correlating-it). A straggler caused by fabric
contention is outside what this design can detect.

### Z — Distributed inference is one workload, not N nodes

Opened 2026-08-21 from the cluster running for real. Every item here is the
same defect wearing a different hat: the dashboard was built when a runtime
lived on one node, and `danflashes` serves **one** vLLM model across two.

- [x] **Z1. Stop flagging cluster workers as unmonitored.** Shipped 2026-08-21.

  `sparkjr` holds 96.8 GiB of a tensor-parallel model and exposes no API,
  because that is what a worker is. `UnmonitoredInferenceRuntime` fired on it
  every poll and **no configuration could ever clear it** — there is no
  endpoint to add. It was being silenced by hand daily, which is the loop
  [W](#w--choosing-which-interfaces-are-monitored--shipped-2026-08-21) broke
  for interfaces reappearing on a different alert.

  This is the exact condition `COLLECTIBLE_RUNTIMES` already exists to avoid
  for Atlas, TGI and ollama — *"with no collector to configure, flagging them
  would produce a warning that can never be resolved, which trains the reader
  to ignore the whole indicator"* — arriving by a route nobody had anticipated.

  **The fix is a scope change, not a new rule.** `detect_unmonitored_runtimes`
  already documented its own principle: flag a runtime only when *nothing at
  all* is configured for it. What changed is what "nothing at all" ranges over
  — this node, or this node's cluster. A runtime collected by any peer in the
  same cluster is not flagged.

  **Computed by the backend, not inferred on the node**, because the agent
  cannot: `cluster` is stamped onto its snapshot by the poller *after* the
  fetch (`poller.py`), so a node genuinely does not know it belongs to one. The
  backend is the only party that can see a node's peers, and it already has a
  channel — `/api/agent-config`, the same path W's interface list rides.

  Sent as `cluster_collected_runtimes`, deliberately **not** merged into
  `runtimes`: those are endpoints the node must poll, and these live on another
  host. "Someone else is already collecting this" is a different fact with a
  different consumer, and merging them would have the agent try to scrape a
  peer's address.

  **It stays honest in the other direction.** Retire the head node's endpoint
  and every node in the cluster flags again, because then no peer is collecting
  it either — pinned by a test, since a suppression that cannot re-arm is just
  a mute with extra steps. Per runtime, too: a cluster collecting vLLM says
  nothing about an unmonitored SGLang server on one of its nodes.

  **The cost, accepted:** a second, genuinely unmonitored vLLM on a cluster
  node is now hidden. The agent already cannot tell two instances apart — a
  process's listening port is not readable across the network namespace, which
  is why the rule was coarse to begin with — so this widens an existing blind
  spot rather than creating one.

  A worker therefore needs no `runtimes:` block at all, which is what
  `cluster.yml.example` now says.

- [x] **Z2. A distributed model reads as one model.** Shipped 2026-08-21.

  `deepseek-v4-flash-0731` is 96.8 GiB on each of two nodes — **193.6 GiB of
  weights**. The dashboard showed 96.8, and `sparkjr`'s identical half carried
  `model=None, server=None` and read as anonymous vLLM memory.

  **The process names carry the topology, which was better news than expected:**
  `VLLM::Worker_TP0` on the head and `VLLM::Worker_TP1` on the worker — tensor
  parallel ranks, declared by vLLM itself. Not relied on for the join (the
  format is vLLM's to change), but it confirmed the shape.

  **The join happens in the backend poller**, for the same reason Z1's
  suppression does: a node does not know it is in a cluster, and certainly not
  what its peers are serving. `attribute_cluster_shards` runs once every node's
  snapshot is in hand, which is the only place the answer exists.

  **Only when unambiguous.** A cluster serving two models through one runtime
  leaves its shards unnamed — the same wall the agent hits with two local
  instances. Attributing 96.8 GiB to the wrong model is worse than leaving it
  unlabelled, because the memory band would then confidently mis-state what is
  holding the pool. An unreachable instance does not name shards either: its
  `model` field carries the endpoint address as a placeholder, and that string
  would otherwise propagate through the band as though it were a model.

  **Attribution is flagged, not silent.** `ProcessInfo.shard` marks a name the
  backend inferred rather than one the process reported, and the process table
  shows it as `·shard`. A reader can tell the two apart.

  **A mistake worth recording, caught while building.** The first design
  collapsed multiple rows for one model into one. It was dead code: a worker has
  no configured endpoint, so it never produced a row in the first place — the
  model always had exactly one row, on the head node, with no size at all. Worse,
  had it ever fired it would have summed **data-parallel replicas**, which are
  copies rather than shards, reporting double the weights actually loaded. The
  footprint is now computed from the GPU process table and summed per cluster,
  which is correct for shards and never sees replicas because they each keep
  their own row.

  The Models table names the cluster rather than one shard's host when a model
  spans nodes, with an `N×` marker whose tooltip lists the hosts — "2 nodes"
  does not tell you which one to go and look at.

  **Still open:** Prometheus does not get this. The `sparkdash_gpu_process_*`
  series come from each agent's own `/metrics`, so a worker's shard is still
  `model=""` there and Grafana sees it unattributed. Fixing that means pushing
  peer model names down to the agent, and model names are live data on a 60s
  config TTL — the lag would show. Noted in `central/grafana/README.md` rather
  than papered over.

- [x] **Z3. `MemoryNearlyFull` asked the wrong question.** Shipped 2026-08-21,
  replaced by `UnexplainedMemoryUse`.

  The old rule fired at >85% used and could not be cleared on a node doing
  exactly what it was built for: `danflashes` holds one 193 GiB model across two
  nodes, so both sit at ~87% indefinitely. Its own description conceded the case
  — *"a node deliberately full of weights looks like this"* — and it was being
  silenced by hand daily, the same loop [W](#w--choosing-which-interfaces-are-monitored--shipped-2026-08-21)
  broke for interfaces and Z1 broke for cluster workers.

  **Measured before replacing it.** Non-model memory — used, minus what
  resident model weights explain — separates the two cases cleanly:

  | node | used | model weights | non-model |
  |---|---|---|---|
  | sparketa | 87.8% | 96.8 GiB | **8.2%** |
  | sparkjr | 86.7% | 96.8 GiB | **7.2%** |
  | sparky | 44.5% | 27.5 GiB | **21.9%** |

  The level rule fired on the two nodes whose memory was *most* accounted for
  and stayed quiet on the one with three times the unexplained footprint. PSI
  was 0.00 on all three, so nothing was suffering.

  **Threshold 40%, and it is PROVISIONAL.** Over 7 days, restricted to samples
  where attribution actually exists, non-model never exceeded 25.8% on any node
  — so 40% is ~1.5× the worst observed. That is 7 days of one node plus hours of
  the other two, not a derived constant like the temperature bands, and the rule
  says so in its own comment.

  **The fallback is a witness, and getting that wrong was the trap.** A naive
  `or 0` for "this node has no LLM processes" makes a *missing* GPU-process
  scrape indistinguishable from genuinely zero model memory — which inflated
  sparky's apparent peak from 25.8% to **39.4%**, almost into the threshold,
  entirely from windows where attribution was absent. The `or` branch is now
  `0 * sparkdash_gpu_utilization_percent`: present whenever the GPU collector
  ran, absent when it failed. So "no LLM processes" yields zero and "collection
  failed" yields no series at all — because missing attribution must not
  manufacture a memory alert. `CollectorFailing` reports the failure itself.

  **Z3 SHIPPED A THIRD PROBLEM, found 2026-08-28.** The replacement rule fired
  **93 times in 7 days** on sparky, and every one was a false positive.

  The expression subtracted only `runtime=~"vllm|llama.cpp|sglang|atlas"`, so
  every *named non-LLM* GPU workload counted as unexplained. sparky runs
  ComfyUI; with 33 GiB resident the rule read **49.5% "unexplained"** for memory
  the dashboard names on its own process table two cards down. The description
  already said "a node full of something nobody can name" — ComfyUI is named.
  The expression simply did not mean what the name and the description both
  said.

  Why Z3's own measurement missed it: the table above was taken with sparky at
  44.5% used and ComfyUI not loaded, so the largest non-LLM consumer on the
  cluster was absent from the data the threshold was calibrated on.

  **Fixed by subtracting all attributed GPU process memory**, which is what
  "explained" means. The witness fallback is untouched — that trap is separate
  and was already guarded by a test that still passes.

  **The threshold needed no change, and that is the evidence it was the
  expression that drifted.** Z3 set 40% at ~1.5× an observed peak of 25.8%; the
  corrected expression peaks at **27.3%** over 7 days and has never exceeded 40%
  on any node. Same ratio, same headroom.

  **What was deliberately NOT folded in, and is now closed:** a named non-model
  workload holding a large share of the pool. ComfyUI reached 48.4 GiB on sparky
  — 40% of the pool — and it is not going to get a rule.

  **Decided 2026-08-28: it is a dashboard fact, not an alert.** Alerts are for
  what you did not choose; the dashboard is for what you did. Running ComfyUI on
  that node is a decision, and a rule that fires because the operator is running
  the software they chose trains them to ignore the channel — the same objection
  that replaced `NodeDiskFillingUp` with a fixed ladder.

  The dashboard already answers it in two places: the memory band splits the
  pool into `models` / `other gpu` / `system` with byte counts, and the header's
  `largest free block` gives the consequence directly — the number that decides
  whether the next model fits. Nothing to build.

  **Z3 SHIPPED HALF A FIX, and the other half was the visible one.** Corrected
  2026-08-21, same day. The alert was only ever one of TWO independent memory
  rules: `common/health.py` computes node health separately, and it kept its
  own level check — so the cards went on reading `serious — memory 88% with
  swap active` while the alert had gone quiet. The dashboard and its alerts
  disagreed about the same nodes, which is worse than either being wrong alone.

  Worse, that rule carried **the exact theory this section had already
  retired**. Its comment read "high usage alone is unremarkable on a box
  deliberately full of model weights, but paired with active swap it means real
  contention" — and `alerts.yml` records the measurement that killed it:
  `swap_used` is a LEVEL, not a flow, so pages parked by some past squeeze sit
  there indefinitely and the conjunct is ~always true. Every node in this
  cluster carries 1.4–2.5 GiB of parked swap, so the escalation was automatic
  and unclearable.

  `assess()` now takes `model_bytes` and asks the same question the alert does,
  against the same 40% threshold from one constant, so the card and the alert
  cannot disagree. Swap escalates nothing: real contention is PSI's job, and
  PSI is a flow. `model_bytes` defaults to 0, so a caller with no process list
  degrades to the old "how full" question rather than silently passing.

  Verified against the live nodes: `sparketa` 87.8% used / 96.8 GiB of weights
  and `sparkjr` 86.7% / 96.8 GiB both go **serious → good**, while `sparky` is
  unchanged.

  **The lesson worth keeping:** "fix the alert" was not the same as "fix the
  thing the operator sees". Two rules for one question, in two files, in two
  languages of intent — and only searching for the *question* rather than the
  *rule name* would have found both.

  **Harm coverage is untouched.** `MemoryPressureHigh`/`Critical` read PSI and
  `SwapThrashing` reads swap I/O; both remain, and a test asserts they do.
  Headroom and harm are different questions, and only the first was unclearable.

### AA — Column widths: sane defaults, then draggable — **shipped 2026-08-21**

Reported 2026-08-21: the Models table shows a large gap between `model` and the
column to its right. All 26 single letters are used, so sections continue as
AA, AB, …

**The cause, and it is not a hard-coded width.** Nothing sets a width on
`model` at all. Numeric columns carry `width: 1%` with `white-space: nowrap`,
which in an auto-layout table is the standard way to say "as narrow as your
content" — so every pixel of slack in the container is handed to the *text*
columns, and auto-layout distributes it in proportion to content, which gives
the lion's share to the column with the longest strings. `model` holds things
like `Qwen_Qwen3.5-4B-Q4_K_M.gguf`, so it wins.

That was tolerable at nine columns. [M3](#m--making-the-tables-usable-at-scale)
restored `size` and `load` and added `prefill`, all numeric — **more `width: 1%`
columns means more slack to redistribute, not less** — and on a wide viewport
the result is the reported gap. The comment above that rule already predicted
the failure mode in the opposite direction ("a wider page spreads every column
equally… the numbers so far from the row's identity that tracking one across
became unreliable"); this is the same problem with the slack pooled in one place
instead of spread evenly.

**So there are two separate things here, and only one of them was asked for.**
Dragging lets a reader fix a bad layout. It does not make the layout good. A
table that needs dragging before it reads well has simply moved the work onto
the reader, so the default has to be right whether or not AA2–AA4 ever ship.

- [x] **AA1. Fix the default.** Shipped 2026-08-21.

  **The plan's own fix would not have worked, and finding that changed it.**
  AA1 was written as "a `max-width` in `ch` on the identity columns". That
  cannot hold, because `app.css` sets `table { width: 100% }` globally: the
  table is *obliged* to fill its container, so capping one column only moves
  the surplus to another. There is no arrangement of `max-width` that makes an
  auto-layout table stop distributing slack — the slack has to go somewhere.

  **So it goes into a trailing empty column.** Every real column takes
  `width: 1%` with `nowrap` — the auto-layout idiom for "as narrow as your
  content allows", which the numeric columns already used — and a final
  `.slack` cell with `width: auto` absorbs whatever `width: 100%` leaves over.
  Columns now size to their own content, and the surplus sits at the end of the
  row where it says nothing and blocks nothing.

  Applied to all four tables rather than only the one reported: the mechanism
  is identical in each, and a table that packs while its neighbour sprawls
  reads as a bug.

  **No ellipsis on the model name, deliberately.** A pathological name widens
  its column and the existing `.scroll` wrapper handles the overflow. This is a
  monitoring table, and a model whose name you cannot read is not an
  improvement on a wide column. `ProcessTable.modelcol` keeps its existing 18ch
  cap — and now actually gets the `nowrap` that its `text-overflow: ellipsis`
  always needed.

  The duplicated `th.r, td.r { width: 1%; white-space: nowrap }` rule went with
  it: the new rule covers every column rather than only the numeric ones, so
  `.r` keeps just the alignment that was uniquely its own.

  `tests/test_table_columns.py` guards the new failure mode — one `<th>` and
  one `<td>` per table, the slack column excluded from the sizing rule, and
  never a declared `ColumnDef` (a reader who could hide it from the column menu
  would get the gap back with no way to understand why). Confirmed to fail on
  an unbalanced pair before shipping.

  **AA2 supersedes all of this**, and that is expected: `table-layout: fixed`
  with explicit per-column widths removes both the slack column and the `1%`
  idiom. This is the smallest change that makes the default readable in the
  meantime, which is what AA1 was for.

- [x] **AA2. `table-layout: fixed`, driven by `ColumnDef`.** Shipped
  2026-08-21. `ColumnDef` gains a required `width` in `ch`, rendered through a
  `<colgroup>` per table.

  **Defaults measured, not guessed.** Longest real content across the live
  cluster: model 22 (`deepseek-v4-flash-0731`), process model 27
  (`Qwen_Qwen3.5-4B-Q4_K_M.gguf`), server 18, RDMA rate 19, interface 13. Each
  column is `max(header, content) + 3ch` for padding and the sort caret.

  **`ch` for the default, pixels for a drag.** `ch` tracks the font, and these
  tables set their own `font-size`; a pixel default would be wrong the moment
  that changed. A dragged width is pixels because that measurement happened at
  a specific size on a specific screen, and storing it as anything else would
  be inventing precision.

  **It reverses AA1's no-ellipsis decision, and that is the real cost.** AA1
  argued a model whose name you cannot read is worse than a wide column — true
  while columns could grow. Under fixed layout they cannot, so the alternative
  to clipping is text visibly spilling across its neighbour, which is worse
  than either. The full value stays reachable, and the column is now draggable
  precisely so a reader who needs the whole name can have it and keep it.

  The `.slack` column from AA1 survives and is still load-bearing: without it,
  fixed layout spreads the surplus across every column in proportion to the
  widths just set, undoing the point of setting them.

- [x] **AA3. The drag handle.** Shipped 2026-08-21 as `ColumnGrip.svelte`.

  **The hazard was the one the plan named**, and it shaped the component: the
  header is already a button. `SortButton` fills the `<th>`, so the grip is a
  sibling that sits above it and stops the gesture at `pointerdown` — the
  button never sees it start, so it cannot complete a click. 8px of grab area
  for a 2px visual cue, because a hairline competing with a button underneath
  is a miserable target.

  **Keyboard resizing shipped with it**, not after: `role="separator"`,
  `aria-orientation="vertical"`, `aria-valuenow`, arrow keys to nudge, shift
  for coarse, Home/Escape to reset. Svelte's a11y lint objects to a focusable
  separator; it is suppressed with the reason rather than downgrading the role
  to `button`, which would be lint-clean and a lie — this performs no action,
  it holds a value.

  Double-click resets one column, which is the escape from the single
  unrecoverable state: dragged so narrow its own handle cannot be grabbed
  again.

- [x] **AA4. Persistence, clamped on read.** Shipped 2026-08-21 alongside the
  hidden-column store, same file and same `reset`.

  Pixels, clamped to `[44, 900]` **at the point of use** rather than on write —
  a width dragged on a 2560px monitor is nonsense on a 1280px laptop and the
  same browser opens both. Fractions of the container were the obvious
  alternative and are worse: hiding a column changes what the container means,
  so stored fractions drift.

  `reset` clears widths as well as visibility, because a column dragged to its
  minimum is hidden in every sense that matters.

  **The tests needed testing.** Two of the four new guards first passed against
  deliberately broken code: one matched `table-layout: fixed` inside the
  comment explaining it after the declaration was deleted, and the other found
  `stopPropagation` in the keyboard handler after the pointer handler had lost
  it — which is the exact bug, since the mouse is what aims at an 8px target
  beside a button. Both now strip comments and check the specific code path.

**Cleanup the switch bought, done 2026-08-21.** AA2 predicted the reserved
`min-width` hacks from [N/S](#n--arranging-the-sections--shipped-2026-08-17)
could go, and they have — ten rules across the three components. They existed
because those columns swing between an em dash and a live reading every time a
model wakes or sleeps, which resized the row on every transition in an
auto-layout table. Under fixed layout a column's width comes from the colgroup
and content cannot change it, so that jitter is impossible by construction
rather than merely discouraged.

Checked before removing rather than assumed: the declared widths are content-
derived and the old minimums were conservative, the widest gap being `tok` at
9ch against an effective ~10.3ch for values like `47.9`. Three stale comments
describing the `width: 1%` idiom went with them, and a sweep confirmed no
orphaned CSS classes were left behind.

**Scope: all four tables or none.** Models, GPU processes and Network's two.
Sorting, pagination and column visibility were each built once in
`TableView`/`ColumnView` and applied everywhere, and a table that resizes while
its neighbour does not is the kind of inconsistency that reads as a bug.

**Deployed and confirmed 2026-08-21.** All four items on the cluster, verified
in a browser: the reported gap is gone and the handles work. The two things no
test could settle — whether an 8px target beside a full-cell sort button is
actually hittable, and whether the clipped names read acceptably now that fixed
layout stops columns growing — both came back fine in use.

**Not planned: auto-fit to content.** A double-click-to-fit gesture is the
obvious companion and is a different feature — it needs measuring rendered text
per column, and the widths would then change as data arrives, which is the
jitter the reserved widths were added to stop.

### AB — Tailwind, weighed and declined; the type scale it would have imposed

Asked 2026-08-21: would Tailwind make this more editable and modular, and was
plain CSS chosen out of ignorance? Measured before answering, because the
question deserves numbers rather than preference.

**What is actually there:** 530 lines in `app.css`, 2,860 across component
`<style>` blocks. **442 of those are comments** — 15% overall, and 26–36% in
the table components.

**Declined, and not out of habit. Three reasons specific to this codebase:**

1. **Svelte already solves what Tailwind mostly solves.** The classic case for
   utilities is a global namespace: styles leak, names collide, nobody dares
   delete a rule. Scoped `<style>` blocks make that impossible by construction,
   so the main benefit is already banked.

2. **The duplication Tailwind would remove is 7.6%.** Of 370 rules, 28 are
   byte-identical across files, and they are one-liners — `.scroll {
   overflow-x: auto }`, `.r { text-align: right }`. The duplication that
   actually matters here is structural, not utility: `AlertHistory` and
   `Settings` share a fly-out shell, and a comment already records the decision
   not to extract it at two users. Tailwind would not have touched that.

3. **The comments are the thing, and utilities have nowhere to put them.**
   442 lines of CSS carry the reasoning for the rule beside them — why a
   reserved width existed, why the numbers must not spread, why one theme
   overrules the house style on pure black. Those are not decoration; they are
   how a decision survives the six months until someone questions it. In a
   `class="w-24 truncate text-right"` string there is nowhere to say *"this
   column swings between an em dash and a live reading every time a model wakes,
   which resized the whole row on every transition"* — and this session
   repeatedly depended on exactly that kind of note being where the code was.

**One more, weaker but real:** `scripts/palette_check.py` parses `app.css`
directly to validate all seven themes for CVD separation, chroma and contrast.
Tailwind v4 handles CSS variables, so the token blocks would survive — but the
validator's contract is "read the stylesheet", and that is simplest when the
stylesheet is the source rather than a build output.

**Where Tailwind would genuinely have helped**, stated so this is not a
one-sided answer:

- **Specificity.** Utilities have none of it. This project has had real
  collisions — `.r { width: 1% }` against `th:not(.slack)` during AA, and a
  documented incident of section rules fighting each other over padding.
- **Consistency by construction.** Which is the finding below, and the part of
  the question worth acting on.

**What a Tailwind migration would actually take**, scoped 2026-08-21 because
"decline" is only an honest answer if the cost of the alternative is known.

**Measured surface:** 20 components with `<style>`, 437 rules, ~598 elements in
markup that would carry utility classes, 530 lines of `app.css`.

**What ports mechanically** — most of it. Flex, spacing, type, colour, borders,
radius. That is the bulk of the 437 rules and the least interesting part.

**What does not, and is where the time goes:**

| feature | count | why it resists |
|---|---|---|
| `var()` references | 322 | every one becomes a theme-aware utility or an arbitrary value |
| custom properties set | 164 | the token layer itself; stays hand-written either way |
| attribute selectors | 44 | `[data-state]`, `[data-tone]`, `[data-link]` — state encoded in markup, which utilities express as conditional class strings |
| descendant/child combinators | 26 | `tbody tr:last-child td` and friends have no utility form |
| `@media` | 20 | Tailwind covers breakpoints; `prefers-reduced-motion` and `prefers-color-scheme` need care |
| transitions | 15 | mostly portable |
| pseudo-elements | 13 | `::after` grips, glyphs, backdrops — arbitrary variants |
| `:not()` / `:has()` | 10 | `th:not(.slack)` is exactly the kind of rule utilities replace by putting the class on the element instead |
| structural pseudo | 8 | `:last-child` borders — `[&:last-child]:` arbitrary variants |
| `@keyframes` | 3 | hand-written in a config either way |

**The theme system is the real obstacle, and it is not a big number.** Seven
`:root[data-theme]` blocks define 164 custom properties, and **two tools parse
that stylesheet as their contract** — `scripts/palette_check.py` and
`tests/test_palettes.py`, which between them validate every theme for CVD
separation, chroma floor, lightness band and contrast, and hold seven text
tokens to 4.5:1. Tailwind v4 consumes CSS variables happily, so the blocks
themselves survive; what needs deciding is whether the validators keep reading
`app.css` or start reading a build output. Reading source is strictly better
and is worth preserving deliberately rather than by accident.

**Honest estimate, in phases that each leave the app working:**

1. **Tokens stay as they are** — Tailwind reads them. Half a day, mostly config
   and proving the seven themes still switch. Nothing else changes.
2. **Leaf components** (`StatusPill`, `Pager`, `SortButton`, `ColumnGrip`,
   `ConnectionState`) — small, self-contained, and the honest test of whether
   the result reads better. A day.
3. **The tables** — `ModelsTable`, `ProcessTable`, `NetworkPanel`. The dense
   ones, and where the comment loss bites hardest: 26–36% of their CSS is
   reasoning. Two days, and the point at which to stop if it feels worse.
4. **The rest** — `App.svelte` alone is 521 lines of CSS with the section grid
   and the layout engine. Two days.
5. **Delete what the migration orphaned**, and re-point the validators. Half a
   day.

**Call it a week of focused work, and the risk is not the week.** It is that
step 3 is where the reasoning has to go somewhere, and there is no good answer
yet for where. Every alternative — a comment above the element, a doc, a
`// why:` convention — is worse than a comment beside the declaration it
explains.

**The cheap 80% is AB1**, which is why it is the item and this is the note. A
type scale is the one thing the comparison showed missing; the rest of what
Tailwind offers here is already provided by scoped styles and the token layer.

**SPIKE RUN 2026-08-21 on `spike/tailwind` — phases 1 and 2, five leaf
components.** Numbers rather than opinion, which was the point of doing it.

| component | before | after | outcome |
|---|---|---|---|
| StatusPill | 49 ln / 27 css | 42 / 0 | full |
| SortButton | 55 / 25 | 43 / 0 | full |
| Pager | 78 / 36 | 41 / 0 | full |
| ConnectionState | 86 / 47 | 89 / 15 | **hybrid, longer** |
| ColumnGrip | 137 / 46 | 132 / 40 | **hybrid** |
| **total** | **405 / 181** | **347 / 55** | −58 lines, −126 css |

**Four findings worth keeping whatever is decided:**

1. **Preflight must not be imported during a phased migration.**
   `@import "tailwindcss"` pulls a global reset that rewrites margins, type,
   borders and form elements everywhere — fine at the *end*, fatal in the
   middle, because it perturbs every unconverted component and makes "did this
   read better?" unanswerable. Importing the layers individually skips it. This
   is the single thing that makes phase-by-phase possible at all.

2. **The theme system survives untouched, and that was the main risk.**
   `@theme inline` maps `--color-ink: var(--ink)` so utilities resolve to the
   project's own tokens. It works *because every theme block targets `:root`* —
   the indirection resolves on the same element the override lands on. All
   seven themes still validate; `palette_check.py` and `test_palettes.py` keep
   reading `app.css` as source, unchanged.

3. **Simple presentational components get genuinely shorter** — Pager 78 → 41
   lines, and the result reads well. **Components with a bespoke animation or
   pseudo-element do not convert**, they go hybrid: `ConnectionState` came out
   *longer* (86 → 89), and `ColumnGrip` kept 40 of its 46 CSS lines. That is
   not a failure to be worked around; it is what a real migration looks like,
   and roughly 40% of these five landed there.

4. **The comment problem is real and reproduced exactly as predicted.**
   `ColumnGrip`'s reasoning — why 8px of grab area for a 2px cue, why invisible
   until wanted — sat above the declarations it explained. Utilities have
   nowhere to put it, so it floated up into a block comment away from the code,
   or would be lost. `StatusPill`'s four `[data-health]` rules became a
   `Record` lookup: greppable from markup, but a missing case is now a silent
   `undefined` in a class string where it used to be a visibly unstyled pill.

**Cost signal:** bundled CSS grew 41 KB → 49 KB with three components fully
converted, because the utility layer is additive while the hand-written CSS is
still there. That reverses as the migration completes, but it does mean a
half-done migration is strictly worse than either end.

**Tried on the cluster 2026-08-21** — the spike build ran as a second backend
on `:8081` against live data, alongside production on `:8080`. **No usability
issues.** That is the expected result and worth saying so: five components
converted correctly should look identical, so the visual test could only have
disproved the migration, never justified it. What it does confirm is that the
mechanics — themes, tokens, utilities, the hybrid components — all work in a
real browser against real data.

**Recommendation: keep the branch, do not merge yet.** The spike answered the
question it was built for — the mechanics work, the themes are safe, and the
gain is real on simple components and absent on complex ones. What it did not
answer is where the reasoning goes, and that is the thing worth solving before
the tables (phase 3), not during.

- [x] **AB1. Promote the type scale to tokens.** — shipped 2026-08-28

  **Shipped, and it was half done already.** The Tailwind migration had added
  `--text-body/label/micro/nano` to `@theme` with 42 uses in converted markup,
  while 81 raw literals stayed in the CSS of everything not yet converted — two
  spellings of one scale, which is the state this item existed to end.

  **The one-offs were adjudicated rather than swept**, which AB1 called the
  actual work:

  | | verdict |
  |---|---|
  | 30 / 22 / 19 / 15px | a real **display scale** — deliberate, and what the page's hierarchy is built from. Named `hero`/`headline`/`title`/`title-sm` rather than folded in, which would have flattened it. |
  | 20px ×2 | one control-glyph role in two components → `--text-glyph` |
  | 13px | **drift, and the proof AB1 was right** — see below |
  | 8px | stays a literal: a sort arrow, `aria-hidden`, a glyph rather than text. Reason recorded at its use and in the test's allow-list. |

  **13px was not in AB1's own table.** It appeared in `ThermalPanel` after that
  table was written and nothing caught it — the exact drift this item predicted,
  arriving while the item sat open. Folded to `--text-body`: it only needs to
  out-size its own 10px label, and 12px does that on-scale.

  Tokens live in `@theme`, so Tailwind v4 generates a `text-*` utility for each
  and markup and CSS name the same size — what a migration would have imposed,
  had for the cost of the tokens. Verified in the **built** CSS rather than
  assumed, because AB2's failure mode is a class that looks right and resolves
  to nothing: `.text-hero{font-size:30px}` and `.text-title-sm{font-size:15px}`
  are emitted and all nine tokens reach the output.

  The diff is mechanical — zero changed lines are anything but a `font-size`,
  and no `var(--text-*)` is used that is not defined. The only intended visual
  change is ThermalPanel's stat values by 1px.

  **Visually confirmed 2026-08-28** against the live cluster: hero figure, node
  titles, cluster memory headlines and the thermal stats all render at their
  intended sizes.

  **That check found two bugs it was not looking for.** Opening the dev server
  surfaced "the alerts and settings buttons don't work" — neither button was at
  fault. Two separate reactive loops each put Svelte into
  `effect_update_depth_exceeded`, and once that fires **nothing on the page
  responds**:

  - `ThermalPanel` wrote `$state` during render. `viewFor(domain)` is called
    from a template expression and set `pageSize` as a side effect of being
    called — `state_unsafe_mutation`. Every other panel already used
    `$effect.pre`; this one was the outlier.
  - `NetworkTable` forced columns on a view it does not own. It renders inside
    `{#each divisions}`, so several instances each computed `tripped` from
    their own rows while sharing one `linkCols`. One division with an error
    wanted `['err']`, one without wanted `[]`, and they overwrote each other
    forever. `force`'s equality guard cannot arbitrate that — it stops a value
    being rewritten with itself, not two callers wanting different values.

  **Both only THREW in dev.** Svelte compiles these checks out of a production
  build, so the same unsafe write and the same fight happen silently there. The
  rule worth keeping: *a component must not write state it does not own on
  evidence only it can see.*

  The measurement that makes the Tailwind question productive rather than
  academic. `app.css` already tokenises spacing (`--step`) and corners
  (`--radius`) and every colour — but **not type**, and the literals betray a
  scale that exists in practice and nowhere in code:

  | size | uses |
  |---|---|
  | `11px` | 36 |
  | `10px` | 30 |
  | `12px` | 21 |
  | `9px` | 9 |
  | 8, 15, 19, 20, 22, 30px | 1–2 each |

  Four sizes carry 96 of 103 uses. That is a scale nobody wrote down, which is
  exactly what Tailwind would have imposed for free — and it can be had here
  for the cost of five tokens, without a migration.

  Name them for role rather than size (`--text-body`, `--text-label`), so the
  eventual answer to "should labels be 10 or 11px" is one edit rather than
  thirty. The seven one-offs are the interesting part of the exercise: each is
  either a considered exception worth a comment or a drift worth folding in,
  and deciding which is the actual work.

  **Not a refactor for its own sake.** It removes a class of inconsistency that
  is currently invisible — nothing today would catch a new component using
  13px — and `tests/test_palettes.py` already shows the shape of the guard that
  could.

- [x] **AB2. The migration, taken anyway — and what it has actually cost.** — closed 2026-08-28

  AB1 above concluded the migration was not worth it and the type scale was
  the cheap 80%. That call was reversed deliberately: the type scale went in
  as part of the spike, the spike tested well in a live container, and the
  decision was to finish rather than keep two spellings indefinitely.

  **All five phases are on `main`** (`baee8b9`), verified against production on
  `:8080` rather than only against the spike's own history. `spike/tailwind` has
  no commits that `main` does not; it can be deleted whenever someone is tidying.

  Phase 4 (App.svelte) is the one that did NOT convert wholesale, and the rule
  it established is the single judgement call in this migration: **an element
  whose class is a selector hook for an ancestor-state rule keeps its styling
  in CSS.** `.node-grid.compact .cluster .nodes` is three levels of context
  before one declaration, at five custom breakpoints (600/900/1100/1160/2320,
  none of them Tailwind's, each the width where a specific thing stops being
  readable). Utilities can express it — `[.node-grid.compact_&]:min-[2320px]:grid-cols-8`
  is valid — but it inverts the reading order and repeats the context once per
  breakpoint per element. Those classes have to survive in the markup anyway,
  so splitting one element's styling between an attribute and a rule is worse
  than either alone. 521 CSS lines became 308, and that is the right number.

  **THE COST IS ONE FAILURE MODE, and it is worth writing down because it
  recurred five times before it was understood.** Utilities carry only what is
  written on the element. Every declaration that used to reach an element from
  somewhere *else* is lost silently when its `class` attribute is rewritten:

  | lost | from | showed up as |
  |---|---|---|
  | `tabular-nums` | global `.num` in app.css | digits reflowing on every poll |
  | `padding-top: 0` | the `th {...}` element selector | header row 1px low |
  | truncation | `th:not(.slack), td:not(.slack)` | headers overflow, no ellipsis |
  | `font-size: 11px` | `.count` in the style block | **rendered at the UA's 16px** |
  | `overflow-x: auto` | `.scroll` in the style block | no horizontal scroll |
  | the whole cell base | `th {...}` / `td {...}` | rules stopping short of the edge |
  | 1px of type size | `.label` resolving across two rules | header labels a pixel large |

  **And one that is the same failure inverted, which is the worst of them.**
  `app.css` sat outside every cascade layer. Unlayered CSS beats EVERY layered
  rule at any specificity, and Tailwind's utilities live in `@layer utilities`
  — so `button { font: inherit; border: none }` silently outranked
  `text-label` and `border-rule` on every button in the app. Both header
  buttons rendered at the UA's 16px with no border **while carrying the classes
  that say otherwise**. It had been true since phase 2 and only surfaced in
  phase 4, because until then every converted component had had its competing
  rules deleted; App.svelte's buttons were the first converted elements a
  surviving global rule still matched. Pager had been quietly unstyled that
  whole time. The element rules now live in `@layer base`; the `:root` blocks
  deliberately do not, since they define custom properties rather than compete
  for declarations.

  None of these errored. None was visible in a diff. Two were caught by tests,
  one by the user's eye, the rest by instrumentation added afterwards.

  **The instrumentation is the deliverable, not the conversions.** Two things
  make this tractable and both should outlive the migration:

  - A **scope-aware dead-class sweep**. A class the markup keeps after its rule
    is deleted renders at browser defaults — and because `app.css` sets a font
    *family* on `body` but never a *size*, a dropped `font-size` lands on 16px
    rather than on the 12px everything else inherits. Scope-awareness is not
    optional: `.count` was present in the built CSS the entire time it was
    broken, as another component's scoped rule.
  - A **before/after computed-style diff**. Snapshot the panel's rendered
    styles keyed by DOM position (not by class — classes are what changes),
    convert, then diff. It found the slack-cell regression within a minute of
    the ProcessTable conversion landing. Keyed by position it also reports live
    data changes as noise, so filter to cells that hold no text of their own.

  **A baseline is only as good as the state it captured.** The phase-4 diff
  flagged five Pager buttons as changed; they were the layering fix REPAIRING
  them, and the snapshot had recorded the broken state as normal. Production on
  `:8080` is the only reference that is not downstream of the migration, and
  every phase should be checked against it before it is called done.

  **What to do before converting anything else:** list every declaration that
  reaches the component from outside its own class attributes — global helpers,
  element selectors, the style block, UA defaults — and name each one in a
  constant. For ProcessTable that list was five items and the conversion landed
  with zero structural diffs against its baseline.

**Revisit if the audience changes.** These reasons are about a project whose
CSS is read more often than it is written, by people who need to know *why*.
A published project attracting drive-by contributions weighs that differently —
utilities lower the cost of a first patch. That is the condition to watch, and
it is [H](#h--genericize-for-distribution)'s question rather than this one's.

### AC — Network graphs

**The fabric is collected and never charted.** Fifteen `sparkdash_network_*`
and `sparkdash_rdma_*` families are exported, scraped and kept for 180 days,
and none of them appear in the history panel. The Network section shows
instantaneous rates in a table, so a link that degraded overnight, a port that
flapped, or a transfer that saturated a 200Gb link at 03:00 leaves nothing you
can look at afterwards. The data is already in Prometheus; only the chart is
missing.

That makes this cheap in collection terms and not cheap in design terms, which
is the whole of the item.

**Shipped 2026-08-23** as a `Network history` section: one chart per interface,
each on its own axis, receive solid and transmit dashed in the node's colour.
AC1, AC1a, AC1b, AC1c, AC2 and AC5 built; AC3 and AC4 decided. RDMA ports that
flapped or stayed down are drawn as stepped two-state charts under the interface
they share a cable with.
Four range queries serve the whole grid however many interfaces it draws, and
the interfaces shown are those that carried traffic in the window — see the
filtering note in AC1, which is the one place this deliberately did NOT reuse
what was already there.

- [x] **AC1. Decide what a network chart is keyed by.** Done 2026-08-23.

  Every existing chip is **one series per node** — GPU utilization, CPU clock,
  memory used. `NODE_FILTERABLE` works by appending `{node="x"}` to a bare
  selector, and `tests/test_history_metrics.py` enforces that the expression
  really is bare, because appending a matcher to an aggregation is not valid
  PromQL and surfaces as a 503 rather than an honest error.

  Network is **per interface**, which is a second dimension no chip has. Summing
  it away is not an option: a 200Gb RoCE link and a 10Gb management port added
  together is a number describing nothing.

  Measured on this cluster, 2026-08-23:

  | | |
  |---|---|
  | interfaces reporting | 14 |
  | `monitored` (not excluded from alerting) | 7 |
  | actually carrying traffic (>1 kB/s) | 7 |

  Fourteen lines on one chart is unreadable; seven is borderline. The `monitored`
  flag already encodes "an interface somebody cares about" and happens to select
  exactly the ones carrying traffic here, so it is the obvious default — but it
  was designed for alerting, and reusing it for charting means one flag serving
  two purposes, which is how a flag starts lying. Worth deciding deliberately
  rather than by convenience.

  **Decided 2026-08-23: small multiples, one chart per interface, each on its
  own axis.** Reasoning below, in AC1b.

  **And the `monitored` flag was NOT reused, on exactly the grounds above.** It
  would have worked — it selects the seven links carrying traffic here, and it
  is already maintained. It was rejected because that flag decides what ALERTS,
  and the first person to silence a noisy port would also, invisibly, lose the
  ability to chart it. The card filters on the data instead: an interface is
  drawn when it carried any traffic at all in the window. That needs no
  maintenance, cannot fall out of date with cluster.yml, and reads correctly at
  both ends — an idle 200Gb link sits at 288 b/s and stays on the page, while a
  wifi port at a true zero drops off it. The hidden ones are counted on a
  control ("3 idle") rather than silently omitted.

- [x] **AC1b. Why small multiples, and not a shared axis of any kind.**

  The instinct is one chart with a line per interface. Measured over 24h on this
  cluster, that does not work — and the reason is not line count:

  | interface | peak (24h) |
  |---|---|
  | `sparky enP7s7` (10Gb management) | **580 Mb/s** |
  | `sparketa enP7s7` | 871 kb/s |
  | `sparketa enp1s0f0np0` | 368 kb/s |
  | `sparketa enP2p1s0f1np1` | 107 kb/s |
  | `sparketa enP2p1s0f0np0` (200Gb RoCE) | **288 b/s** |
  | `wlP9s9` x3 | 0 |

  **Six orders of magnitude.** On a shared linear axis, one management port
  doing 580 Mb/s flattens every other link — including the 200Gb fabric — onto
  zero. The chart would then say the interconnect is idle, which is a stronger
  and more wrong claim than drawing nothing.

  **Grouping does not fix it**, which was the first idea and is worth recording
  as rejected on evidence: the spread is *within* classes, not between them.
  580 Mb/s against 871 kb/s is two ports of the same 10Gb model; 368 kb/s
  against 288 b/s is two RoCE links on the same node. Splitting fabric from
  management halves the line count and leaves a 1000x range in each chart.

  **Percent of link capacity** was the other candidate and remains attractive
  for one specific question — "is anything saturated" — since it puts 10Gb and
  200Gb on one bounded axis. It fails the general case for the same reason:
  everything idle sits on zero, indistinguishable. It also cannot include the
  three wifi ports, which report no speed. Worth revisiting as a *second* chart
  rather than the primary one.

  Small multiples scale each link to its own traffic, so an idle 200Gb link
  still shows its shape and a saturated one still reads as saturated. The axis
  label carries the magnitude, which is what makes cross-chart comparison
  possible at all.

  **The cost, stated plainly:** chart count grows with interfaces rather than
  staying fixed the way the existing per-metric charts do — 14 here, filtered to
  7 by `monitored`. At 32 nodes this does not hold and something else is needed.
  Accepted for now because the alternative misrepresents the fabric today, and a
  32-node install is hypothetical while a flat-lined 200Gb link is not.

- [x] **AC1c. Export the RDMA-to-interface pairing as a label. Done 2026-08-23.**

  `sparkdash_rdma_port_info` carries `device`, `port`, `link_layer` and `rate`
  but **not** the Ethernet interface the RoCE device is paired with. The agent
  knows it — `RdmaPort.interface` drives the UI and the paired alert exclusion —
  it is simply not exported.

  Two things need it. Grouping or filtering interfaces by fabric role is not
  expressible in PromQL without it. And AC3's double-counting cannot even be
  *detected* in a query: there is no way to tell which interface an RDMA counter
  duplicates, because the join key is absent.

  The device names encode the pairing (`roceP2p1s0f0` against `enP2p1s0f0np0`),
  so it is derivable by string munging. That is the wrong fix — the agent has
  the real answer and should say it.

  **Shipped as a label on `rdma_port_info`.** Adding a label splits a series'
  history at the deploy, which is exactly why `network_monitored` is a separate
  family rather than a label on `network_up`. The trade is right here and wrong
  there: this series is always 1, so its history holds nothing to lose, and the
  old label set ages out of the 180-day window on its own. A port with no netdev
  gets the label EMPTY rather than omitted — a family carrying two different
  label sets is something Prometheus accepts and every `on (interface)` join
  then silently drops.

- [x] **AC1a. It gets its own card, not more chips on History. Done
  2026-08-23.**

  History already carries **15 metrics** and its chip row wraps to three lines
  at full width. Six or so network chips would not fit that row; they would
  bury it.

  A separate section also gives the fabric its own time range, which is the
  behaviour you want: correlating a 200Gb link saturating at 03:00 against GPU
  temperature means looking at two windows, not one. And it inherits everything
  the section machinery already does — drag to reorder, pair side by side,
  collapse, hide, per-section row counts.

  The cost is one entry in `SECTIONS`, a branch in `App.svelte`'s section
  renderer, and a `DEFAULT_ROWS` entry. Existing saved layouts are already
  handled: `reconcile()` appends a section a saved order has never seen rather
  than dropping it, so it appears for people who arranged their page before
  this shipped, in its default position.

  Open: whether it reuses `Trends` with a different metric list, or is its own
  component. Reuse is the obvious first answer, but `Trends` currently assumes
  one series per node — see AC1.

- [x] **AC2. Bits, and the counter that is typed as a gauge.** Done 2026-08-23.

  Network gear is rated in bits and the Network panel already converts — a chart
  that showed bytes would disagree with the table above it. So the query is
  `rate(...bytes_total[window]) * 8`.

  These `_total` series are real monotonic counters read from sysfs, but the
  agent exports them through a gauge family, so Grafana will not suggest
  `rate()` and PromQL linters object to it. `rate()` is correct — they reset
  only on host reboot, which `rate()` handles. Already documented in
  `central/grafana/README.md`; noted here because the history layer has never
  had to `rate()` anything, and `MetricSpec` has no field that says "this needs
  a rate".

  **One thing this turned up, worth recording because it looks like the
  established idiom and is not.** `tokens_per_second` sums several families with
  `sum by (node) ({__name__=~"..."})`, adopted specifically to stop binary `+`
  dropping a node that runs one engine and not another. Reaching for the same
  shape on the fault counters fails: `rate()` DROPS `__name__`, so
  `rate({__name__=~"..._(receive|transmit)_errors_total"}[w])` reduces two
  families to two series with the identical label set, and Prometheus refuses
  with "vector cannot contain metrics with the same labelset" — a 422, measured
  rather than reasoned about. The regex form only works over there because
  nothing takes a rate, so `__name__` survives to keep the series apart.

  So the fault queries use `+` after all, and the difference is real rather than
  a lapse: engines are OPTIONAL and per-node, which is what makes `+` lossy
  there. Directions are not — the agent emits receive and transmit for every
  interface in one loop, unconditionally. That invariant is what makes the
  expression correct, so it is now pinned by
  `test_both_directions_are_always_emitted` rather than left as a property
  someone has to notice.

- [x] **AC3. Decide whether RDMA gets its own chart, and avoid drawing the same
  bytes twice.** Decided 2026-08-23.

  `sparkdash_rdma_receive_bytes_total` is read from the Ethernet interface the
  RoCE device is paired with, because mlx5 leaves the IB-style counters at zero.
  So RDMA throughput and interface throughput are **the same bytes**, and
  charting both would show one transfer as two.

  Either the RDMA chip charts something the interface chip cannot — port state
  over time, or negotiated rate against actual — or there is no RDMA chip and
  the interface chart carries it, with the RoCE pairing shown in the table as it
  is now.

  **Decided: no RDMA throughput chart.** The bytes are the interface's bytes,
  and two charts of one transfer is not a second measurement — it is the same
  one drawn twice, on a card whose whole argument is that a misleading chart is
  worse than no chart. The RoCE pairing stays in the table, and AC1c means a
  query can now *prove* the duplication rather than take this paragraph's word
  for it.

  **What is left unbuilt, deliberately.** Port state over time is the RDMA
  question the interface charts genuinely cannot answer: a port that flapped at
  03:00 shows up in `rdma_port_active` and nowhere else. It wants a step plot of
  a 0/1 series, and `MetricChart` draws neither steps nor a two-value axis — a
  line chart of a boolean, auto-scaled to [0, 1], reads as a wild oscillation
  when it is one clean transition. Carried forward as AC5 rather than bolted on
  badly.

- [x] **AC4. Decide whether errors and drops are charted at all.** Decided
  2026-08-23.

  They read zero on a healthy fabric, and a chart of a flat zero is noise
  competing for the same screen space as a chart that moves. The table already
  handles this better: `err` and `drop` are `signal` columns that force
  themselves back into view on their first non-zero value, so a fault announces
  itself without a chart.

  The argument for charting them anyway is **when** — a table says errors exist,
  a chart says they started at 02:14 and stopped, which is the difference
  between "a cable is bad" and "something happened during the backup window".
  That is a real question the table cannot answer, so this is genuinely open
  rather than rhetorical.

  **Decided: charted, but signal-gated** — the same rule the `err` and `drop`
  columns already follow. A fault chart is built for an interface only when
  something in the window is non-zero, and it sits immediately after that
  interface's throughput chart rather than in a block at the end. On a healthy
  fabric that is fourteen charts which do not exist; on a bad cable it is one
  that appears beside the traffic it interrupted, which is what makes "were the
  errors while it was busy" answerable in a glance.

  Errors and drops stay SEPARATE lines rather than one "faults" total. A drop is
  usually backpressure — a queue that overflowed under load — and an error is
  usually physical. Summing them saves one line and loses the distinction that
  decides whether anyone walks to the rack.

- [x] **AC5. RDMA port state over time.** Done 2026-08-23.

  The one fabric question the throughput charts cannot answer. A RoCE port that
  dropped and came back at 03:00 leaves `rdma_port_active` stepping 1 → 0 → 1
  and leaves the byte counters looking like an ordinary quiet spell, so the
  chart that would show it is the one not built. Split out of AC3 because it
  needs a renderer this card does not have: a step plot on a two-value axis.
  Drawn as an ordinary auto-scaled line, one clean transition reads as a wild
  oscillation between the top and bottom of the plot.

  Cheap-ish once that exists — the series is exported and scraped today, and
  `rdma_port_info.interface` (AC1c) is what lets such a chart sit beside the
  interface it shares a cable with rather than in a section of its own.

  **Built, and it was not hypothetical.** Measured over 7d before writing any of
  it: `changes(sparkdash_rdma_port_active[7d])` reports 3-4 transitions each on
  four ports, and two of sparky's ports have read 0 for the entire week. Every
  throughput chart over the same window looks unremarkable, which is exactly the
  gap this was supposed to close.

  Signal-gated like the fault charts, with one extra rule. A port that was up
  for the whole window is not drawn — that is a chart-sized restatement of the
  green dot on the Network table. A port that was DOWN for the whole window very
  much is: the table shows a red dot and cannot say it has been a week.

  Placement is where AC1c earns its keep. The chart sits directly under the
  throughput of the interface it shares a cable with, so a port dropping can be
  read against the traffic on the same wire at the same instant. An agent from
  before AC1c reports no pairing; those charts go to the end of the grid rather
  than being dropped, since hiding a flapping port on the deployment running the
  older agent is the worst place to hide one. One exception to the idle filter
  falls out of this: an interface with no traffic whose RoCE port flapped stays
  on the card. A silent link whose port dropped is not an idle link.

  **Two measurements changed the implementation.**

  The 0/1 series is drawn STEPPED with `align: 1`, not interpolated. A sample
  says what the state was at that instant and it held until the next scrape said
  otherwise; a straight line between two samples asserts the port spent the
  interval passing through states it cannot occupy. The axis is fixed with two
  splits labelled `down` / `up` — auto-scaled, one clean transition fills the
  plot and reads as a wild oscillation, and a 0.5 gridline offers a value that
  cannot occur.

  And the query has to AGGREGATE, which was not obvious. A `cluster` label was
  added to the targets part way through the retention window, so 12 of 18
  node/interface keys have two series apiece — sequential, never overlapping
  (max concurrent series per key is 1, checked). The frontend merges the two
  variants into one history rather than keeping either: taking one would
  truncate the chart at the relabel, which looks precisely like the port having
  stopped reporting. The same merge covers the agent upgrade that ships
  AC1c, where one variant carries `interface` and the other predates it.

  Worth noting for the existing queries: that same check is what confirms
  `sum by (node, interface)` cannot double-count on the throughput charts.

- [x] **AC6. The overview table, so the card stops growing with the cluster.**
  Done 2026-08-23.

  AC1b accepted small multiples on the record that "at 32 nodes this does not
  hold and something else is needed". This is that something.

  Measured before designing anything: a fully-populated GB10 has 6 interfaces
  and 4 RDMA ports, so 32 nodes is ~190 links and, at 7d with faults and port
  states, ~500 charts. The wall is **not the data** — five range queries serve
  the card however many links it draws — it is uPlot INSTANCES, one canvas and
  one ResizeObserver each. Which is why the sparklines in the table are SVG
  paths and nothing else; a canvas per row would rebuild the exact cost the
  table exists to escape.

  **Two candidates died on contact with the data.** Percent-of-link-capacity is
  the obvious "is anything important" summary, and over 24h the busiest link
  here peaks at **5.8%** of its rating while every other link sits at
  **0.0001%** — eleven flat zeros. Left out until links actually fill.
  Burstiness works where that fails: peak-over-mean separates into 18.4-20.3 /
  1.5-1.8 / 1.1 with an order of magnitude of clear air, so the threshold of 4
  has a factor of four of margin on both sides.

  **Ranked in lexicographic tiers, not a weighted score.** A weighted sum needs
  three invented constants and makes "why is this row third" unanswerable; a
  tier is a sentence. State, then bursty, then steady — and the `why` column
  says which rule placed the row, because a ranking nobody can account for reads
  as the data being wrong.

  Volume is never the first key: alone it ranks a busy management port above
  every fabric link, every time. Burst is only a key INSIDE the bursty tier,
  which the first version got wrong and the deployed page showed at once — it
  ordered the steady RoCE links 74, 76, 77, 79 kb/s, ascending, because ratios
  of 1.12 against 1.09 decided it and volume never got a turn.

  **One query had to be added after all**, against the plan's claim that none
  would: `network_link_up`, with `min_over_time` so a flap inside a 900s bucket
  is not sampled away. On the chart grid a down interface carries no traffic and
  was filtered out as idle, so the question never arose; in a table, where
  nothing is hidden, it would have been a flat zero row with no way to tell a
  quiet wire from an unplugged one. It found the three wifi ports are DOWN
  rather than idle.

  **Open: automatic promotion.** Ranking plus the `why` column answers "what
  needs attention" without opening anything, so a chart that promotes itself was
  left out — worth revisiting once the ranking has been used in anger.

- [x] **AC7. The live Network card stops being a second interface table.**
  Done 2026-08-23.

  Once AC6 shipped, the obvious question was whether the live card was still
  earning its place. Half of it was not: **six of the interfaces table's seven
  columns** — name, node, rx, tx, err, drop — were already in the history table,
  which also carries trend, peak, `why` and link-down. Only `link` was unique.

  The RDMA table is a different matter and stays. It is **per `device:port`**
  where history collapses to one row per interface with `roce` as worst-of, so
  on a node with four RoCE devices the history row can say something is down and
  not which. It carries **`physical_state`** separately from `state`. And it
  carries the **negotiated rate string** — `200 Gb/sec (2X NDR)` against
  `100 Gb/sec (4X EDR)` on the f1 ports — which is an info label, has no history
  query behind it, and is the specific ConnectX-7-came-up-slow failure the
  column exists for.

  Two facts moved rather than being lost: the negotiated **speed**, and the
  **`monitored`** flag. Both come off the live snapshot, and both are safe to
  read live and apply to a window in a way that reading live THROUGHPUT would
  not be, because neither is a rate. Note the flag is now SHOWN here while still
  never being filtered ON — AC1 rejected reusing it to decide what the card
  draws, and reporting it is the opposite of that: it tells a reader why a bad
  link is not paging anyone.

  **One argument that did not survive measurement.** The expectation was that
  freshness would decide it — a 2s direct poll against a 240s rate window. Live
  and the table's `now` column were measured against each other on the same
  links and agree within about 1.5x on steady traffic. The gap only opens on a
  burst, where the window flattens the spike. Real, but narrow, and not what
  justifies keeping the card.

  What the live card still uniquely gives up nothing on: it polls agents
  directly, so it works when Prometheus is unreachable — the failure this
  project already builds `data_age_s` for — and its err/drop counters are
  cumulative since boot, answering "has this link EVER errored", which no window
  can.

**No scale ceiling for throughput.** `MetricSpec.scaleMax` exists so a quiet
hour does not auto-scale into looking dramatic, and it is set where the hardware
gives a natural ceiling (100°C, 300W, 3003MHz). Link speed looks like such a
ceiling and is not: `sparkdash_network_speed_mbps` differs per interface.
Measured here, three speeds across fourteen interfaces — 4x 200Gb, 4x 100Gb,
3x 10Gb — so one fixed axis sized for the RoCE links would flatten a saturated
management port into a flat line at the bottom. Throughput charts already fall back to the window maximum for
this reason, and network should too.

### AD — Every temperature the box exposes — **shipped 2026-08-25**

**The dashboard reported two temperatures. The hardware exposes 18–23**, all of
them already scraped by node_exporter, in Prometheus, and retained 180 days
with nothing drawing them. The same shape the network families were in before
AC.

Not academic. Measured over 24h before writing anything: **`acpitz` zone0 peaked
at 95.4 °C while the GPU read 72.0 °C at the same instant** — a sensor 23
degrees hotter than the one the dashboard led with.

| source | count | hardware threshold | now / 24h peak |
|---|---|---|---|
| `acpitz` zones 0–6 | 7 | 104.8 °C kernel trip | 47–69 / **95.4** |
| `nvme` | 3 | max 82.85, **crit 84.85** | 44–52 |
| `mlx5` NIC asic | 4 (absent on sparky) | **crit 105** | 52 |
| wifi phy | 1 | none | 42 |
| GPU (NVML) | 1 | slowdown 86, shutdown 90 | — / 85 |

- [x] **AD1. Five measured facts, each of which killed an obvious idea.**

  1. **No fan, power, voltage or current sensors exist.** lm-sensors sees three
     chips and every one is temperature-only. There is no airflow or wattage
     story on this hardware, and nothing here should grow one without someone
     first checking the sensors appeared.
  2. **The `Processor` cooling state has never left 0 in 7 days**, on any node.
     ACPI passive throttling is not the mechanism — the GPU throttles itself at
     86 °C. A throttle-state chart would be a flat zero.
  3. **`PCIe_Port_Link_Speed_000f` reports −231.** Junk from sysfs; excluded.
  4. **The 7 zones are double-exposed.** `hwmon0` *is* `thermal_zone0`'s hwmon
     child and republishes all seven as temp1–temp7 while `/sys/class/thermal`
     publishes the same seven. Reading both counts every package sensor twice —
     which inflates nothing visibly, because the max is still the max, and
     silently doubles every row count.
  5. **Averaging is meaningless.** One box holds 95.4 °C and 58 °C at once.
     Every aggregate here is a **max**.

  Also: the zones are unlabelled and all correlate 0.89–0.99 with GPU
  temperature, because a GB10 is one package. There is no chassis-versus-die
  separation to find — the useful split is by **component domain**.

- [x] **AD2. The agent is the single source, and the cost of that.**

  Classification and limits live in `collectors/thermal.py`; everything reads
  its series. The node-card headline must be live and the live path is a direct
  agent poll by design; the agent already reads the zone trip points that
  `node_hwmon_temp_crit_celsius` does not carry; and domain classification is a
  judgement that drifts the moment it exists in two places.

  **The cost, stated plainly: none of it lights up until the node stacks are
  redeployed.**

- [x] **AD3. Headroom, not temperature, is the ranking.**

  The limits differ by twenty degrees across one box. Sorted by degrees an
  85 °C GPU heads the table and a 52 °C NIC sits at the bottom; by headroom the
  GPU has 5 degrees left and the NIC has 53. That inversion is pinned by a test
  that asserts both orderings explicitly, because it is the entire reason the
  limits are collected.

  A sensor stating **no** limit sorts LAST, never first. No known margin is not
  the same as no margin left, and putting an unmeasurable wifi radio above a
  GPU five degrees from shutdown would be the exact inversion this prevents.

  Two headlines, not one: **hottest** and **closest to its limit** are usually
  different sensors. Showing one and calling it "system temperature" is what
  the old single CPU number did.

- [x] **AD4. Verified against node_exporter on live hardware.**

  The classification could silently drop or duplicate a chip and no unit test
  would know. node_exporter walks the same sysfs independently, so it is the
  second opinion: on sparketa the collector reported **15 sensors, 15 distinct
  names, 15/15 agreeing within 2 °C, and a sensor-count difference of 0** once
  node_exporter's own zone republication is removed. Added as a section to
  `scripts/validate-on-gx10.sh`, where a maintainer would look.

- [x] **AD5. Alerting — shipped 2026-08-28.**

  `CpuTemperatureHigh` reached `pending` **413 times on sparky in 7 days and
  never fired** — the spike always ends before the hold time — and
  `TemperatureBandsNotDerived` has fired on sparkjr. Both are real and neither
  is a drawing problem. Worth doing now that the sensors are visible, and worth
  doing separately: the question is hold times and thresholds, not charts.

  **Measured 2026-08-28, and the problem was bigger and the diagnosis slightly
  wrong.** It is not one rule, and it is not the threshold.

  | alert | pending, 7d | fired |
  |---|---|---|
  | `GpuTemperatureHigh` | 891 | **0** |
  | `CpuTemperatureHigh` | 463 | **0** |
  | `GpuThrottled` | 59 | 0 |
  | `GpuTemperatureCritical` | 22 | 0 |

  **These sensors spike; they do not plateau.** In sparky's hottest 40 minutes
  of the week the CPU was above its band **51% of the time**, oscillating
  between 88.1 and 95.1 °C — and the longest CONTINUOUS run above it was **120
  seconds**, against a 10 minute hold. Every dip reset the timer. The recorded
  diagnosis ("the spike always ends before the hold time") reads as *brief
  spikes*; measured at minute granularity sparky had episodes of 14, 19 and 26
  minutes. The heat lasts. It is the *continuity* the rule required that never
  existed.

  **Fixed by smoothing the input, not shortening the hold.** Both warnings now
  ask what fraction of the last 10 minutes was spent above the node's own band,
  and fire when that exceeds half. Shortening the hold was the tempting fix and
  is wrong: the observed runs clear a 2 minute hold, so it would have fired on
  single transient spikes instead.

  **>0.5 was measured, not chosen.** A stricter >0.8 **never occurred** on any
  node in 7 days — it would have been a second rule that cannot fire.

  Holds calibrated against the same 7 days: CPU `for: 10m` → 2 alerts/week, GPU
  `for: 15m` → 3, both only on sparky. The GPU's is longer deliberately — it is
  the part that is supposed to run hot, at 10m it fires 5x/week and would be
  tuned out by the second week, and `GpuThrottled` already covers the
  consequence at critical severity.

  **Replayed against the real 08-24 event** rather than trusting the arithmetic:
  CPU would have fired twice on sparky, GPU three times, and both stayed silent
  on sparketa and sparkjr. Right now, cool, both evaluate to 0.

  **A bug this change introduced, caught before shipping:** the expression
  changed what `$value` means. It is a fraction of a window now, and the
  existing annotation would have sent "CPU on sparky at 1C" to a phone. Both
  summaries now use `humanizePercentage`.

  **The criticals are deliberately untouched.** They also go pending and never
  fire, and for them that is correct — the GPU touches its 86 °C slowdown for a
  handful of 15s samples and backs off, which is the transient a hold exists to
  filter. A rule quiet because the condition does not happen is not the same as
  one that cannot fire when it does. `tests/test_thermal_alerts.py` pins that
  distinction so a later sweep for "rules that never fire" does not merge them.

  **`TemperatureBandsNotDerived` needs nothing.** All six bands across three
  nodes are hardware-derived (`nvml-slowdown`, `acpi-critical-trip`); it has 0
  pending and 0 firing in 7 days. The sparkjr firing recorded above predates
  the window and appears to have been a bring-up transient.

  **Not addressed: `GpuThrottled`**, 59 pending and 0 fired, is the same shape —
  a spiky state under a continuous 5m hold. It is a clocks rule rather than a
  temperature one and its threshold question is different (its own description
  says GB10 throttling is usually power delivery, not heat), so it is left for
  a separate look rather than swept in here.

### AE — Card sizing: a second layout regime, not just a drag handle

Planned 2026-08-28, reframed 2026-08-29. The ask started as "draggable sizing"
and landed somewhere more interesting: **a tall card on the left beside two
short cards on the right**, with a toggle between that and today's behaviour.

**That is one side of a trade this project has already made once, deliberately.**
`96a00f4` replaced independent column stacks with a grid of rows, and recorded
why:

> With the columns filling independently, the second card on the left began
> beside the MIDDLE of the first card on the right — two lists side by side that
> happened to finish level, rather than a grid of rows.

and, in the same message:

> This is the row-height **stranding** the original three-zone design avoided,
> and it is now deliberate: it only appears inside a band, which is a place two
> cards were explicitly put side by side.

So today's layout knowingly wastes vertical space to buy alignment. The new
regime spends alignment to reclaim the space. Neither is wrong; they suit
different content, which is exactly what makes a toggle the honest answer rather
than a hedge.

| | **Aligned rows** (today) | **Packed columns** (new) |
|---|---|---|
| `left[n]` and `right[n]` | share a row and a height | independent |
| a short card beside a tall one | stretches, leaving slack | keeps its height |
| tall left, two short right | impossible | the point |
| scanning across | rows line up | nothing lines up |

- [x] **AE1. The toggle is a CSS switch, and that is the whole trick.** The
  machinery that creates alignment is two declarations, and packed mode is their
  absence:

      .cols  { grid-template-rows: repeat(var(--band-rows, 1), auto); }
      .zone  { grid-row: 1 / -1; grid-template-rows: subgrid; }

  Drop both and the zones are independent stacks again. `--band-rows` simply
  goes unused. This is not a rework of the band model: bands still form the same
  way, the page is still a sequence, and pairing by dragging onto an edge is
  untouched.

  **Drop targeting survives, and that was not luck.** `96a00f4` chose subgrid
  over one flat grid of cards precisely so the columns stay separate elements —
  *"the drag targeting aims at a zone, and a flat grid has no column to aim
  at."* Packed mode keeps zones as elements too, so the gesture that arranges
  the page works identically in both regimes.

- [x] **AE2. Which is the default, and where the toggle lives.** **Decided
  2026-08-29: aligned stays the default and packed is opt-in** — it is what
  exists, and a layout regime is not something to change under a reader who did
  not ask. Height ships alongside rather than after. Settings
  already has `Full / Compact` for node cards; this is the same shape of choice
  one level up. Naming it for what it buys rather than how it works — *Aligned*
  vs *Packed* — is what makes it choosable without reading this file.

- [x] **AE3. Per-card height — and it means something in BOTH modes.** The
  plan said height "only means something in packed mode," on the reasoning that
  aligned mode fixes a card's height to the band's. That is half right: the
  band's height is the TALLER card's content, so making the taller card's plots
  taller still grows the band, and making the shorter one's taller does nothing
  visible until it becomes the taller. Height is therefore a live control in
  both modes; packed is where it is *only* about the card you are dragging.

  What shipped: **charts, not tables**, exactly as the plan divided them.

  - **tables** were left alone. `maxRows` already answers "how tall" from
    settings, and a drag beside it would be two controls for one thing.
  - **charts** were the real gap, though not where the plan looked. The
    `height: 140px` in `Trends.svelte` is the card-level "no data in this
    range" placeholder; the plots themselves came from `height = 132`, a prop
    default in `MetricChart`. Both surfaces now pass a dragged height:
    `DEFAULT_PLOT_PX` in the layout store, clamped to 80–480, keyed per section
    in `spark-dash.plot-heights.v1`, and `MetricChart` imports the same
    constant for its own default rather than repeating the number.

  ONE GRIP PER CARD, not per plot. Charts that share an x axis and not a height
  stop being small multiples, which is the entire reason they are a grid.
  `RowGrip` sits at the foot of the grid and reports the count it moves —
  *"Applies to all 11 charts"* — because a grid of eleven grows by eleven times
  what the pointer travelled, and without the warning the card appears to leap.

  **"Drag until they match" was NOT built, deliberately.** The snap exists to
  make equal heights reachable in packed mode — but aligned mode already
  delivers equal heights exactly, for free, and it is the default. An
  approximate snap that reproduces in packed mode what the default mode does
  precisely is a second mechanism for a solved problem. It is also harder than
  it sounds: what the reader wants level is the CARD's bottom edge, and a card
  is header + controls + plots + pager, so the snap would have to solve for a
  plot height from a card height across four terms that each change on their
  own.

  Measured on a live band, one plot: drag 132 → 257px, `ArrowDown` +16,
  `Shift+ArrowUp` −64, clamped at 480 and 80, double-click back to 132 with the
  stored entry removed — and the section placement untouched throughout, which
  is the thing AE5's hazard note is about.

- [x] **AE6. The corner, because the bar at the bottom was not findable.**
  Shipped to production, and the first person to use it in packed mode went
  looking for the control and could not find it. Two things were wrong, and the
  second is the one worth remembering.

  1. **It was in the wrong place.** A 48px bar at the bottom CENTRE of the chart
     grid, invisible until hover. People look for a resize control in the
     bottom-RIGHT corner, because that is where every window and every textarea
     puts one. It is there now, and it stays faintly visible at rest — a
     deliberate exception to how this page treats card controls, since the move
     handle and fold chevron are both hover-to-reveal. Hover-to-reveal only
     works if you already know to hover, and the failure mode is a control
     nobody finds.
  2. **It was on two cards out of seven.** AE3 divided charts from tables and
     shipped only charts, on the reasoning that `maxRows` already answers "how
     tall" for a table and a drag beside it would be two controls for one thing.
     That reasoning holds for a SETTINGS entry and does not hold for a corner:
     the answer to "where is it on the Models card" was "nowhere". So the grip
     lives in `Section`, every card has one, and on a table it MOVES the row cap
     rather than sitting beside it — which is what AE3's own note said to do if
     height ever shipped for tables.

  Three things fell out of making one gesture serve both units:

  - **`ROW_CHOICES` stopped being the validator.** `readRows` accepted only the
    seven offered caps, so every dragged value — 13 rows — was silently
    discarded on reload, the card simply back at its default. It clamps to a
    range now. The settings `<select>` grew `rowOptions()` for the same reason:
    a `<select>` whose value matches no option renders BLANK and resets the card
    to the first choice the moment it is touched.
  - **`0` is a sentinel and arithmetic must not reach it.** Measured bug: forty
    `ArrowUp`s from 12 rows landed on `all rows` — shrinking a card as far as it
    goes made it show everything, at twice the size. `dragRows()` floors every
    gesture at `MIN_ROWS`; picking "all" stays deliberate, from the list.
  - **A ResizeObserver was the WRONG SIGNAL** for "what is this card drawing?".
    System Activity's plot replaces a 140px placeholder with a 132px plot and
    its caption, so the card's box lands within a pixel or two of where it
    started and the observer never fires again — the card went on reporting
    itself as a table of rows minutes after it had drawn a chart. What changes
    is the DOM, so a MutationObserver watches that instead, coalesced to one
    read per frame because a paging table mutates on every poll.

  The delta is divided by what the card actually grows by — rows of the chart
  grid, or tables sharing the cap — so the corner follows the pointer instead of
  running from it. Temperatures moves five tables at once; the earlier version
  had to warn *"applies to all 11 charts"* in its label to explain the leap.

  Measured live: Models 10 → 14 rows by drag, persisted off-list; Temperatures
  8 → 7 across five tables; `ArrowDown` +1, `Shift+ArrowDown` +5, clamped at 1
  and 200, double-click back to the default with the entry removed — and the
  section order untouched through 340 arrow presses, which is the hazard.

- [x] **AE7. The vertical rhythm, and packed becomes the default.** Packed mode
  shipped, ran in production for a day, and the verdict was that it *relocates*
  the whitespace rather than removing it. Measured on one band of seven cards:

  | | left | right | |
  |---|---|---|---|
  | packed | 1276px | 1806px | **530px of dead column** |
  | aligned | — | — | **1342px** hidden *inside* three cards |

  Aligned stretched `RDMA ports` from 280 to 1040px, `System Activity` by 345,
  `Model activity` by 237. **Decided:** every card resizes in increments of one
  table row so the columns share a vertical grid; content that no longer fits
  paginates; packed is the default.

  It came out much smaller than planned. Dragging a table card already moved it
  a whole row at a time, and pagination already followed — so what was actually
  missing was the *grid* and the *snap*, not a new height-as-master store. Both
  zones are now `grid-auto-rows: var(--row-unit)` with each card spanning a
  whole number of them, and the pointer travel is quantised before it is scaled.

  **The row had to be told its height first.** It was 25px by accident:
  `py-[5px]` around a 12px font at `line-height: normal`, where "normal" is
  whatever the font's own metrics say. Fine for a table, useless as a layout
  unit — a fallback font would have shifted every card on the page. It now
  states its arithmetic in `app.css`.

  Three bugs, all found by measuring rather than looking:

  - **`getComputedStyle().getPropertyValue('--row-unit')` returns the literal
    `calc(...)`.** Custom properties are substituted, not computed, so
    `parseFloat` gave NaN and the `|| 25` beside it hid that the token was never
    read. A probe element resolves it the way CSS does.
  - **The coalescer deadlocked in a background tab.** `requestAnimationFrame`
    does not run in a hidden or occluded tab, so one notification arriving there
    set the `queued` flag, the frame never came, and every later mutation
    returned early *for the life of the component* — a card's span froze at 4
    while the card grew to 668px and overlapped its neighbour. A dashboard on a
    second monitor is exactly where that happens.
  - **`align-self: start` is load-bearing.** Without it the card fills its span,
    so the next measurement reads the span as the new natural height and grows
    it again every frame. `effect_update_depth_exceeded` is compiled out of
    production builds: it throws in dev and spins silently for a reader.

  Aligned mode is unchanged and still available — verified still pairing by row
  (668/668, 1108/1108, 317/317) — it is just no longer what you get by default.

  Verified live: all seven cards report `onGrid` and `fits`, tops at
  0/350/650/1000 and 0/700/1775; a chart card steps 13→14→15→16 rows for three
  key presses (+25px each) and 13→18 for a five-module drag.

- [x] **AE8. A card can be held taller than its content.** AE7 made a card's
  span `ceil(content)`, which is the right default and the wrong ceiling:
  dragging past the last row did nothing at all. Models has eleven models, so
  once the cap passed eleven the card stopped dead under the pointer. With two
  independent columns, holding a card taller than it needs is the *only* way to
  line their bottoms up, which is what packed mode gave up when it stopped
  stretching them.

  So the gesture now writes two things. The content control — a row cap or a
  plot height — makes the card genuinely taller while it still has something to
  show; the held span keeps it that tall once it does not. The card renders
  `max(natural, held)`, so while content is growing the held value sits below it
  and contributes nothing.

  **The held value is only ever user input, and that is load-bearing.** A span
  derived from the measured card is read back on the next pass as the new
  natural height and grows again every frame. A constant is not: the card
  measures `max(natural, held)`, `cardRows` reads that back as exactly `held`,
  and it stops. `min-height` is therefore driven by `--held-rows` and never by
  anything `Section` measured.

  **Bug found while testing it: a click pinned the card.** Every pointermove
  that had not yet crossed a module boundary — including the one a plain click
  produces — wrote `startSpan + 0`, silently holding the card wherever it was.
  Found by noticing stored spans for four cards nobody had dragged, after clicks
  that landed on or near their corners. A held height is a deliberate act and
  now takes a deliberate gesture.

  **And a guard that passed against deleted code.** The test asserting the card
  fills its held height read the raw file, and the comment above `min-height`
  explains why `max(0px, …)` is needed — so it contains the string "min-height"
  and the guard passed with the rule removed. This file's own header says
  comments are stripped before every check; three CSS guards were not doing it.
  `css_block()` now does.

  Measured live: Models held at 959px with eleven rows shown and the card's own
  border extending the full height; shrunk to 159px with the table paginated to
  two rows; double-click back to 358px with both stored entries gone.

- [x] **AE9. Even gaps: the card fills its span.** Reported from a screenshot,
  then measured: gaps between consecutive cards of **18, 21, 23, 32 and 34px**
  where there had been a uniform 16. A regression introduced by AE7.

  The cause is the quantisation itself. A card left at its natural height inside
  a taller span puts the slack — up to one whole module — into the gap *below*
  it, so the spacing is 16px plus however much that card happened to round up
  by, and no two match. Filling the span moves the slack inside the card, under
  its own content where it reads as padding, and every gap becomes the 16px the
  margin declares.

  **Filling it is what made the measurement hard, and the failure is a RATCHET
  rather than a runaway.** The card now fills its span, so reading its rendered
  height reads the span straight back — and a span that only ever came from its
  own output can never go down. A card whose content shrank would keep the
  height it once needed for ever. So `measure()` lifts `min-height` for the
  length of one read; `getBoundingClientRect()` flushes layout, so the value is
  real and the restore lands in the same frame.

  `align-self: start` and `min-height` are not in tension, which is worth
  stating because they look it. Start-alignment is what makes the lifted
  measurement return the CONTENT's height instead of the grid area's;
  `min-height` is what makes the card occupy the whole span. Remove either and
  the gaps go uneven again, for opposite reasons.

  The held height folded into the same number — `cardRows = max(natural, held)`
  — so there is now one span driving one `min-height`, rather than two heights
  competing.

  Verified live: every gap exactly 16px with all cards still on the grid;
  Network Activity switched charts → table → charts at 684 → 609 → 684px, so
  the ratchet is genuinely gone; a card grown to 834px and dragged back landed
  on 334px, its starting height.

- [x] **AE10. Aligned mode deleted, and the grid becomes the layout.** Aligned
  was one half of a trade the module grid dissolved: it bought rows that line up
  across a band by *stretching* the shorter card, and the grid lines them up
  without stretching anything. Keeping a second layout regime that is strictly
  worse is a second thing to maintain and a second thing to explain, so it is
  gone — `BandMode`, `bandMode`, `setBandMode`, `readBandMode`, the storage key,
  the `subgrid` rule, `--band-rows` and `Band.rows`, and the Settings control.

  **Deleting it exposed a regression the flag had been hiding.** A full-width
  band is a zone too, and it renders *outside* `.cols` — so `.cols.packed >
  .zone` never matched it. Full-width cards were laid out with the old
  `grid-auto-rows: auto` while their slots still carried the span and the 16px
  margin, which stacked on `.sections`' own gap: **32px between full-width cards
  against 16px between cards in a column.** Nobody could have named it; everyone
  would have seen it.

  So the module grid moved to `.zone`, which is every zone, and `.sections`
  gives up its gap — each card carries its own, so no container may add another.
  The rhythm now runs down the whole page instead of restarting at each band.

  Verified live: every gap on the page exactly 16px, in both a page of
  full-width bands and a two-column band; every card top on the module in both
  columns (offsets 0/13/25/39 and 0/28/73); resize still 359 → 759 → 359 with
  the gaps holding.

- [x] **AE11. Temperatures: the slack goes to the bar, and the domains pair
  up.** Reported as "a ton of wasted space to the right of the temperatures
  card", and measured at **154px of an 817px card — 19%** in a half-width
  column, more on a full-width one.

  The spacer was deliberate: columns declare their widths in `ch` (87ch ≈
  629px) and a trailing `<col />` with no width absorbs the difference so the
  declared widths do not stretch. Because it takes *whatever remains*, it grew
  with the card.

  **Two corrections to the premise, both measured before building anything:**

  - **Removing the note text frees no space.** Every division heading is a
    single 18px line and the notes never wrapped, so the line stays regardless
    for the domain name and count. It is a decluttering decision, not the thing
    that recovers the room.
  - **Two sensor blocks need ~1272px**, so they cannot fit in a half-width
    column at all — and a card's width no longer follows the viewport, since the
    same window shows this card at 817px in a column and 1649px full width.

  So: the **bar** takes the leftover width at every size (a longer track is a
  finer reading of headroom), and the domains pair two-across under a
  **container query** — the first in this codebase, and warranted precisely
  because the viewport stopped being a proxy for a card's width.

  **The spacer was load-bearing in two cases** and dropping it outright brings
  back the stretching it prevented: `bar` can be switched off from the
  ColumnMenu, and it can be given a pixel width by its ColumnGrip. The rule is
  therefore conditional — the flexible column is `bar` when it is visible *and*
  unpinned, otherwise the spacer returns, in all three of `<colgroup>`,
  `<thead>` and `<tbody>` (two out of three is a malformed table, not a
  narrower one).

  The notes come off the heading and survive as its `title`, so the reason the
  GPU limit reads 90° where the package reads 104.8° is still one hover away.

  **Hazard handled:** `Section.measure()` scaled the resize drag by the NUMBER
  of tables. Five stacked domains grow the card by five rows per press; the same
  five paired two-across grow it by three. It counts distinct `offsetTop` now —
  the same technique the chart grid already used — so one expression covers both
  layouts.

  Measured live. Half width: `to limit` 116 → 270px, column widths summing to
  783 = the table width, no spacer. Full width (1649px): domains pair
  Package|GPU, Storage|Network, Wireless, and the card goes **1109 → 709px, a
  36% cut**. Bar pinned to 120px and bar hidden both bring the spacer back with
  every other column keeping its `ch` width. Module grid intact throughout —
  every gap 16px, every card top on the module — and the corner still moves the
  card exactly 25px per press.

- [x] **AE12. The corner drags sideways too: half width ↔ full width.** Asked
  for after the height resize proved itself, and it was mostly already built —
  `layout.toggleWidth()` already existed, already reversible, already
  remembering the column a card came from in `lastColumn`. It was simply only
  reachable from Settings. All that was missing was the gesture.

  **The one hard part is that the two axes are not the same kind of thing.**
  Height is continuous — many rows, tracking the pointer, nudgeable. Width is a
  single flip with a large consequence. Letting a diagonal do both is not untidy
  but WRONG: changing the width changes the card's content layout (Temperatures
  pairs its domains when wide), which changes its natural height, which
  invalidates the span and the scale `onstart` captured. So the gesture **locks
  to one axis** after 8px and ignores the other for its life. That also stops a
  horizontal wobble during a height drag from reflowing the page under the hand.

  The width half **aims and commits on release**, never mid-drag — the rule
  `Section` already states for moving a card, and a width flip rearranges more
  than a move does. 48px of travel to arm it, six times the axis-lock threshold,
  because a card jumping between half and full width should take some saying.
  The cue is an `outline` on the card: painted outside the box, so showing the
  intent cannot move the thing being aimed at.

  **The held height is cleared on a width change** (Brian's call). A card pinned
  to 45 rows at half width is absurd at full width, where its content reflows
  shorter.

  **Bug found while testing it:** `pointerup` and `pointercancel` were bound to
  one handler, so an interrupted gesture — the browser taking the pointer back,
  a touch leaving the screen — flipped the card anyway. Height never needed that
  care because it applies as it goes; width is aimed, so it has something to
  abandon. Same reasoning as `Section.onCancel`.

  Inert below 1100px, where the zones stack and every card is full width
  whatever its placement says; the cursor drops back to `ns-resize` there rather
  than promising a drag that does nothing. ARIA keeps the height contract — a
  `separator` has one orientation and one value, and two axes are not expressible
  — with `ArrowLeft`/`ArrowRight` on the grip for width alongside the existing
  Settings toggle.

  Coexists with the pair gesture (dragging a full-width card onto another's
  edge), which was a deliberate decision: they are not identical, since pairing
  picks a partner and a side while this returns you to your last column.

  **Follow-up: the cue drew the wrong box.** Shipped outlining the CARD, which
  says "something will change" and not "it will become this wide" — and width is
  the whole point of the gesture. It draws the target footprint now: the full
  page width when widening, and the actual column box when narrowing, measured
  from a live band rather than computed, because the band is right there and
  arithmetic would be a second definition of the same geometry.

  `columnFor()` came out of `toggleWidth()` so the preview and the move share
  one answer — a cue that worked out its own destination could disagree with the
  move it was previewing.

  **And a class-name collision worth remembering:**
  `document.querySelector('.sections')` finds *Settings'* own three
  `<ol class="sections">` first — they come earlier in the document and measure
  0×0, so the preview rendered as a 2px sliver at the window's left edge. It
  walks up from the card with `closest()` now. A class name is not a unique
  address.

  Verified against reality rather than by eye: ghost 1649px at left 32 vs card
  1649px at left 32 when widening; 817 at 32 when narrowing to the left column;
  817 at **865** when narrowing to the right one.

  Measured live: half → full → half by drag; a (30,150) diagonal resized height
  only, zone unchanged; a (120,30) diagonal flipped width only; 20px of travel
  did nothing and 80px armed; cancel abandoned; a half-width card aimed narrower
  and a full-width one aimed wider both stayed dark; keyboard did both ways; at
  1000px wide nothing armed and the cursor read `ns-resize`. Module grid intact
  throughout and the section order untouched.

- [x] **AE13. Settings gives up the size controls it no longer owns.** The
  width toggle and the rows-before-paging select answered exactly the two
  questions the resize corner now answers, and answered them worse: from a
  fly-out that covers the page, where neither can actually be judged. Both are
  gone, and with them everything that existed only to serve them —
  `rowOptions()`, `ROW_CHOICES`, `PAGED_SECTIONS`, and about thirty lines of CSS
  for a three-control row that is now one control.

  **Show/hide stays**, and the distinction is worth stating: it is not a size
  question, and unlike size it has no gesture — a hidden card has no corner to
  drag, so the panel is the only place it can live.

  The section note now says where the controls went, rather than leaving a
  reader hunting for something that moved.

  **One capability goes with it: `0`, the uncapped "all" sentinel.** It was only
  ever reachable from that select, and a drag is floored at `MIN_ROWS` on
  purpose so that shrinking a card can never flip it to "show everything". In
  practice this costs nothing — a drag reaches `MAX_ROWS` = 200 and the largest
  table on this dashboard is 43 sensors — but a layout saved with a `0` still
  round-trips, because `rowsFor` keeps translating it to `Infinity`.

- [ ] **AE4. Column width as an fr ratio, if it is still wanted.** Now clearly
  distinct from AE12, which moves a card between half and full; this would
  change how a band SPLITS its two columns. Separate from the above and
  smaller. One constraint is not negotiable: **`minmax(0, 1fr)` must survive.**
  A bare `1fr` is `minmax(auto, 1fr)`, whose `auto` minimum lets content drag
  the tracks — measured, the two "equal halves" were **813.273px and
  769.727px**, one column having taken 43px from the other by holding wider
  tables. That is the whole of the dashboard's horizontal layout shift. A
  resize therefore sets the fr RATIO, never a pixel width.

  It also needs clamping, for the reason the 1100px breakpoint already exists:
  below that, zones stack because *"a half-width table on a laptop is
  unreadable"*. A column dragged to 20% recreates that at any viewport.

- [x] **AE5. Reuse `ColumnGrip`'s contract.** AA settled what a resize gesture
  owes and the comments are explicit: `role="separator"` because *"keyboard
  resizing is not optional"*, a 16px step with shift for coarse, and a reset as
  *"the escape from a column dragged too narrow to grab again."* A card dragged
  to nothing has the same trap.

  The hazard differs, though. `ColumnGrip` was shaped so a resize could not
  reach the sort button underneath; here the thing underneath is the
  **drag-to-move handle and the band drop targeting**, so a mis-aimed resize
  would rearrange the page rather than merely re-sort a table.

  Shipped as `RowGrip`: same `role="separator"` (horizontal), same 16px step
  with shift for coarse, same Home/Escape/double-click reset. The hazard was
  handled the way `ColumnGrip` handles its own — `stopPropagation` on
  pointerdown, so the move gesture never begins — and on keydown as well, which
  `ColumnGrip` did not need: `Section` listens for Escape on the WINDOW to
  abandon a move, and its handle moves the card on ArrowUp/ArrowDown, so both
  of this grip's key bindings collide with one of its neighbour's.

  Both failures are silent by construction — a propagating pointerdown makes
  the card fly toward a zone with no error anywhere — so they are pinned by
  `tests/test_plot_height.py`, verified by mutation rather than by passing.

**Explicitly NOT this:** a 12-column grid or per-card pixel widths. The band
model is carefully built and a general grid would replace its reasoning rather
than extend it.

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

- [x] **J1. `central/compose.single-host.yaml`.** Shipped 2026-08-21, and it
  turned out to be a correctness fix rather than a footprint one.

  The roadmap said the central exporter "duplicates the node stack's". It is
  worse: `prometheus.yml` hardcodes `node: sparkmon` on that job, so on one box
  it invents **a node that does not exist** — ~640 series describing the GB10
  under another machine's name, in the same label space as the real one, giving
  every `by (node)` query a phantom member.

  **An overlay, not a second stack**, using `deploy.replicas: 0` to drop the
  service. Both mechanisms were dry-run on a node first: a `profiles` override
  works too, but would have to be declared in `compose.yaml`, and every
  existing deployment would silently lose the exporter the first time it
  deployed without the profile set. `replicas: 0` leaves the base file
  untouched, so multi-host installs cannot regress.

  **The job is dropped, not re-pointed** — my first attempt pointed it at the
  node stack's exporter, which would have scraped one target under two job
  names and duplicated ~4,750 series to no purpose.

  **`PrometheusStorageFillingUp` moved to its own rule file**, because it pins
  itself to the host holding the TSDB by job name and that host differs. Only
  that one rule is duplicated; `alerts.yml` stays shared through the same
  mount. The single-host variant drops the pin, which is safe there and only
  there: with one host, `{mountpoint="/"}` matches exactly one series.

  **The two are not interchangeable, and it is loud.** Running the single-host
  rule against the live three-node Prometheus returns `found duplicate series
  for the match group {}` and stops evaluating — which is the original bug the
  job pin was added to fix, reproduced deliberately to confirm the guard.

- [x] **J2. `cluster.yml.single-host.example`.** Shipped 2026-08-21.

  One file, one purpose: `host:` must be the machine's LAN IP, never
  `localhost`. The backend resolves it from inside a container, so `localhost`
  is the container — and the failure is the most misleading available:
  everything starts cleanly and the dashboard reports the box you are sitting
  at as unreachable, apparently blaming the agent. `BACKEND_URL` in the node
  stack's `.env` crosses the same boundary.

- [x] **J3. The trade, stated in docs/deployment.md.** Shipped 2026-08-21,
  directly under *Why not on a GX10* so the two are read together — that
  section is not wrong on one box, it is **accepted**.

  The footprint is a measured table rather than a claim, re-measured on the
  running three-node install: node stack ~78 MiB, central stack ~178 MiB, a
  single-host total of **~220 MiB — about 0.18% of a 121.7 GiB pool**. The
  original figures (2026-08-16, one node) are superseded; the TSDB has grown
  from 79 MB / 4.3k series to 285 MB / 9.2k.

- [x] **J4. The cost, live in the dashboard.** Shipped 2026-08-21 — **after
  discovering the roadmap's premise for it was wrong.**

  J4 assumed "the GPU process table already attributes memory per process, so a
  single-host install can show what monitoring costs". It cannot: that table
  lists **NVML** processes, and the monitoring stack holds no GPU memory at
  all. Checked against a live node — nine GPU processes, every one of them
  ComfyUI or llama.cpp.

  What made it cheap instead: **every component already measures itself.**
  `process_resident_memory_bytes` is standard in every Prometheus client, and
  Prometheus, Alertmanager and node_exporter all exported it already. The agent
  was the only gap, because a bare `CollectorRegistry` does not carry what the
  default registry does — one line.

  **Self-reporting is the design, not a shortcut.** The alternative — a
  collector identifying "the monitoring processes" by name — is the ComfyUI
  problem again: `python` names nothing, and a wrong match would bill someone's
  model to monitoring, which is precisely the number this exists to make
  trustworthy.

  Shown in the settings fly-out, where the deployment's other facts live, and
  it reads **194.7 MiB** on the live install. Null rather than zero when
  Prometheus cannot answer: zero is a claim that monitoring is free, which is
  the one answer that is never true.

**Multi-arch images: considered and declined 2026-08-21.** The single-host path
is where a published amd64 backend would meet an arm64 GB10, so it is the right
place to have asked. Declined because every documented install BUILDS rather
than pulls, and the registry path is reached only by deliberately setting
`BACKEND_IMAGE` — an explicit choice about which image you want. Manifest lists
would reintroduce the buildx/QEMU machinery that native building exists to
avoid, for a case that only arises off the documented path.

The case that does exist belongs to the maintainer, not a user: these stacks
deploy `:latest` with `PULL_POLICY=always`, so building the backend on a GB10
while testing single-host and pushing it would have the amd64 VM pull arm64 on
its next deploy. `publish-images.sh`'s architecture guard is the answer to that
— which makes the two decisions mutually supporting rather than alternatives.
Reasoning in full at [docs/deployment.md](deployment.md#building-and-shipping-images).

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

- [x] **H1a.** Six functional lines carry MY values. Measured 2026-08-18 —
  the raw grep finds 118 hits of `192.168.50.x` and 15 of the hostname, but
  everything else is commented out (`SPARK_NODES=`, `LLAMA_ROUTER_URLS=`,
  `LLAMA_METRICS_ROUTERS=`) or a docstring example (`inventory.py`). Those read
  as examples and stay:

  | File | Line | Value |
  |---|---|---|
  | `central/compose.yaml` | 113 | default image |
  | `node/compose.yaml` | 54 | default image |
  | `central/.env.example` | 61 | `BACKEND_IMAGE=` |
  | `node/.env.example` | 38 | `AGENT_IMAGE=` |
  | `central/.env.example` | 90 | `ALERTMANAGER_EXTERNAL_URL=` |
  | `node/.env.example` | 47 | `BACKEND_URL=` |

  The two addresses become placeholders obvious enough that leaving one
  unedited fails loudly rather than quietly pointing at somebody else's LAN.
  The four image lines are H1b, because a string swap alone makes them worse.

- [x] **H1b.** `pull_policy: always` has to become
  `pull_policy: ${PULL_POLICY:-missing}` in the same change. This is the part
  H1 originally missed.

  `pull_policy: always` is set on our two services and is correct for a
  floating tag in a registry — Compose's default `missing` would use any
  locally-present image forever and report a no-op deploy as success. But the
  compose comment already records the collision:

  > `publish-images.sh --no-push` builds an image this will then ignore,
  > because it always prefers the registry.

  That is exactly the stranger's path: clone, build, `up -d`. And swapping the
  default to an unqualified name like `spark-dash-agent:latest` **makes it
  worse** — `always` resolves that to `docker.io/library/spark-dash-agent`, so
  instead of failing against an unreachable LAN registry it reaches Docker Hub
  and either errors or pulls an unrelated image squatting the name. Trading an
  unreachable-registry failure for a supply-chain one is not a fix.

  With `${PULL_POLICY:-missing}`, a fresh clone uses the image it just built
  and never contacts a registry; `.env` sets `PULL_POLICY=always` for the
  registry path, preserving today's measured behaviour verbatim.
- [x] **H2.** `scripts/publish-images.sh` no longer hardcodes a registry.
  `REGISTRY` and `OWNER` now default to whatever the clone's own `origin`
  remote points at, so a fork publishes to its own registry with no
  configuration and nothing personal is committed. Env vars still override for
  the case where the registry is not where the source lives.

  Also gained `--no-push` (build with no registry or login at all, which is
  what an evaluator needs), `--tag`, `--no-latest` and `--help`. Documented in
  [deployment.md](deployment.md#building-and-shipping-images).
- [x] **H3.** Sweep the READMEs for `/docker/spark-dash-homegrown` and
  `192.168.50.x` used as *instruction* rather than *example*. The distinction
  is whether a reader would paste it.

  Counted 2026-08-18: 8 hits of the path, and the original guess that "most are
  illustrative" was wrong. Five are instruction-shaped — `REPO=/docker/...` as a
  pasteable shell assignment in `central/README.md:32`, `central/README.md:438`
  and `node/README.md:23,80`, plus `cd /docker/spark-dash-homegrown/central` at
  `central/README.md:45`. Prose mentions (`central/README.md:406`,
  `node/README.md:66`, `docs/deployment.md:265`) are fine as-is.

  The fix is to make `REPO` a variable the reader sets once, rather than a path
  they inherit — the surrounding scripts already read `$REPO`.
- [x] **H4.** A quickstart that a stranger can follow end to end, which is the
  real test of whether H1–H3 are done — the current READMEs assume the reader
  is me. Top-level `README.md` is currently a title and a `## Docs` index, so
  there is no wrong-shaped page to fight; the quickstart is new writing.

  It has to be walked, not just written: clone into a path that is *not*
  `/docker/spark-dash-homegrown`, with no `.env` files and no registry login,
  and follow it literally. Anything that only works because of local state is
  the bug H4 exists to catch.

- [x] **H5.** Add a `LICENSE`. There isn't one, which was not previously noted
  anywhere in this section. Without it the default is all-rights-reserved: a
  public repo would be readable but not legally usable, which defeats the point
  of publishing it. This is a real gate, not polish.

**Shipped 2026-08-18, and walked rather than asserted.** The H4 test was run
for real on the monitoring VM: the working tree was extracted to `/tmp/h4walk`
(deliberately not `/docker/spark-dash-homegrown`), `git init` + an `origin`
pointing at `https://github.com/someone/...` to imitate a stranger's clone, no
`.env`, no `docker login`. Results:

- `publish-images.sh backend --no-push` ignored that GitHub origin and produced
  `spark-dash-backend:latest` + a sha tag — the fix at work; before it, the
  origin would have named the image `github.com/someone/spark-dash-backend`.
- `docker compose config` in both stacks resolved to
  `spark-dash-{backend,agent}:latest` with `pull_policy: missing`, and with
  `PULL_POLICY=always` + a registry image set, to `always`. Both paths verified
  against real Compose, not reasoned about.
- Exactly one `CHANGE_ME` per stack: `ALERTMANAGER_EXTERNAL_URL` in central,
  `BACKEND_URL` in node. Placeholders use `.invalid`, which RFC 2606 guarantees
  will never resolve, so an unedited one fails loudly.

**Now verified. Walked 2026-08-22**, with production stopped and its four
containers renamed aside so the shipped `container_name` values were free —
nothing tracked was edited and the window was 19 seconds. Following the
quickstart literally, `docker compose up -d` came up clean: all four containers
running, backend healthy, Prometheus scraping 4 targets, and `/health`
answering `{"prometheus":"ok","alertmanager":"ok","nodes_up":1}`. `degraded`
was correct — the example config names a vLLM endpoint that is not running.

Two things the walk taught that reading could not:

- **A bare `up -d` that skips the quickstart's `chown` crash-loops Prometheus**
  — root-owned bind mounts against uid 65534, panicking in
  `NewActiveQueryTracker`. That is not a defect: the README says so in bold and
  says skipping it "reads like a Prometheus bug". The first walk skipped those
  lines and reproduced the documented consequence, which tests the warning
  rather than the software. Worth recording because the failure looks like a
  product bug and is not one.
- **H1a's audit missed the most important file.** See H6.

- [x] **H6. `cluster.yml.example` shipped a live node.** Found by the H4 walk,
  2026-08-22.

  H1a's table listed six functional lines and asserted everything else was
  "commented out or a docstring example". `central/cluster.yml.example` was
  never in that table, and its ACTIVE block read:

  ```yaml
  nodes:
    - id: sparky
      host: 192.168.50.61
  ```

  Not commented — the config a stranger `cp`s as step one of the quickstart. The
  walk proved the consequence rather than arguing it: a clean clone came up
  monitoring the maintainer's `sparky` and reported its agent version.

  Its sibling `cluster.yml.single-host.example` had already been genericized by
  [J](#j--single-host-profile-everything-on-one-gb10); the main file simply
  never got the same pass. Both now use `CHANGE-ME` and a `CHANGE_ME.invalid`
  host, which RFC 2606 guarantees never resolves — so an unedited copy reports
  the node unreachable instead of quietly pointing at whatever answers on the
  reader's own LAN. The single-host file's `192.168.1.100` was replaced for the
  same reason: plausible and resolvable is worse than obviously wrong.

  `tests/test_distribution.py` now guards every `*.example` and both compose
  files against personal values in uncommented lines, and against a host
  placeholder that could resolve. Verified against all three regressions.

**MIGRATION, applies to THIS deployment.** `pull_policy` is now
`${PULL_POLICY:-missing}` rather than a hardcoded `always`. An existing `.env`
has no `PULL_POLICY`, so after pulling this change a stack tracking a registry
`:latest` silently stops fetching new builds — the exact stale-image failure
the old hardcoded value existed to prevent, and it reports success while doing
it. **Add `PULL_POLICY=always` to every live `.env` that points at a registry.**
Stacks pinned to a sha are unaffected — but **neither of ours is**, and the
first version of this note said otherwise. Corrected 2026-08-18.

Both live stacks track `forgejo.indielab.tech/brian/spark-dash-{agent,backend}:latest`
with no `PULL_POLICY`, so both stop updating the moment the new compose reaches
them. The earlier claim that they pinned shas came from reading
`/docker/spark-dash-stack-{central,node}/.env` — archived directories that
still exist, still parse, and are wired to nothing. The `.env` that counts is
the one Dockhand deploys, `/docker/hawser/<stack>/.env`; confirm with

    docker inspect <container> \
      --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'

which answers in hawser's own namespace (`/data/stacks/<stack>`). A stale copy
of a config file is worse than a missing one: it answers the question
confidently and wrongly.

**H1 gates publication; the rest is polish.** `forgejo.indielab.tech` is the
*default image* in both compose files, not just documentation — so an
unconfigured clone pulls from a registry the user cannot reach. That has to be
a local-build default before anyone else runs this. The other items can follow.

**Scope: de-personalization, NOT hardware abstraction. Decided 2026-08-18.**
"Genericize" could have meant making the GB10 constants configurable — the
unified-memory pressure bands, the measured trip temperatures, the 2411MHz
real clock target, the 8-slot node palette. It explicitly does not.

Those constants are correct *because* of the hardware. The memory bands only
mean anything where there is no separate VRAM; the trip points are hardcoded
precisely because NVML reports `N/A` for them. Turning them into settings would
convert measured, defensible reasoning into configuration nobody else has any
way to fill in correctly — and a repo that says "this is a GB10/DGX Spark
dashboard, here is why each threshold is what it is" is worth more than a
generic one that has forgotten why. Running elsewhere is [J](#j--single-host-profile-everything-on-one-gb10)'s
job, and stays there.

So `sparky`, `gx10-1`, `spark2` and the RFC1918 addresses in *commented* lines
and docstrings all stay. They read as examples, and the H3 test — would a
reader paste this? — says no.

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
- [x] Optional Grafana pointed at the same Prometheus for ad hoc exploration.
  Superseded by [X](#x--grafana-as-a-first-class-consumer), which shipped a
  starter dashboard and the metric catalogue behind it. Whether Grafana gets a
  *container here* is X4 and is still open on purpose.
- [~] Job-level drill-down (per-request tracing), if useful.
  **RETIRED 2026-08-28.** "If useful" was doing the work in that sentence and
  three months of use has not made it useful. Per-request tracing is a
  different product from a node dashboard; the engines' own metrics already
  answer queue depth and throughput.
- [x] **`dcgm-exporter` / `dgx-spark-prometheus` — WILL NOT SHIP.** Decided
  2026-08-21, after being deferred through three phases. Closing it as a
  decision is worth more than carrying it as a maybe.

  DCGM's headline advantage is per-GPU memory accounting, which is exactly what
  unified memory breaks on GB10 — a resident daemon for its weakest feature
  here. What it would genuinely add is **memory bandwidth**, the one blind spot
  left: NVML reports 0% memory utilization while the GPU sits at 96%, and
  `DCGM_FI_PROF_DRAM_ACTIVE` is the only remaining route to it.

  Three things settle it against:

  - **Not installed, and would have to be.** Checked on a GB10 2026-08-21: no
    `dcgmi`, no `nv-hostengine`, no `libdcgm`, driver 580.173.02. A daemon on
    every node plus an exporter, not a container to add.
  - **It spends the node budget on monitoring**, against a stack measured at
    ~78 MiB per node precisely so the box stays free for models — and
    profiling counters carry overhead on the GPU itself.
  - **Whether GB10 exposes the counters at all is unknown**, so the work opens
    with an experiment that might answer "no".

  If the bandwidth question ever becomes live, the spike is one node and one
  command: install DCGM on a single box, `dcgmi dmon -e` against the profiling
  field IDs, and see whether DRAM activity is reported. "No" would be a real
  result worth recording. Adding a daemon to chase a number nobody has needed
  is the wrong direction until something is actually unexplained.

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
3. ~~**`dcgm-exporter` vs. `dgx-spark-prometheus`.**~~ **Settled 2026-08-21:
   neither. Will not ship.** `spark-dash-agent` reads NVML directly for the
   basics, and DCGM's headline advantage (GPU memory) is what unified memory
   breaks on GB10 — a resident daemon for its weakest feature here. Memory
   bandwidth stays a known blind spot rather than being chased with one. See
   [H](#h--genericize-for-distribution) and
   [deployment.md](deployment.md#gpu-baseline-exporter--will-not-ship-decided-2026-08-21).
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
