# Features

Everything spark-dash shows and does, in full. The [README](../README.md) has
the short version and the install.

## What it monitors

**77 metric families per node**, live at 1–2s over a WebSocket and kept for 180
days in Prometheus. Read-only: it can't load, unload or kill anything.

**The GPU, as GB10 actually reports it.**

- Utilization, temperature, power, clocks.
- **Unified memory, read correctly.** NVML reports a total that never moves on
  GB10, so generic exporters show a flat line. The agent reads `/proc/meminfo`.
- **Throttle state** — `IDLE` / `PASS` / `LOCKED` / `THROTTLED`, judged only
  under load. On GB10 a throttle usually means power, not heat.
- **Memory pressure (PSI).** Contention shows up here before swap does.

**Which process is holding the memory.** Per-process GPU attribution, labelled
with the runtime that owns it — llama.cpp, vLLM, SGLang, TensorFold, ComfyUI. "97 GiB used"
becomes "97 GiB held by these two vLLM shards".

**Per-model inference state.**

- llama.cpp router: which models are loaded, sleeping or unloaded, slots in use,
  KV-cache occupancy, and a timeline of every load and unload.
- vLLM, SGLang and TensorFold are scraped directly by Prometheus, so their full metric
  surface is stored whether or not this UI draws it.
- **Decode and prefill counted separately.** Combined, they once read 47,672
  tok/s while the model was generating 48.
- **Prefix-cache hits and speculative decoding.** The Models table's column
  menu adds `prefix hit`, `accept` and `accept len` (mean tokens per
  verification step), since the engine started. System Activity charts `KV
  cache` (the fullest model per node), `Prefix hits` and `Draft accept` over
  the chart's window. llama.cpp and vLLM report all three; TensorFold has no
  prefix cache and no step count; SGLang's are live figures only. On current
  llama.cpp, KV is the context of requests in flight.
- **Time to first token** in the header, p50 and p95 for the busiest model,
  with every model and its speculative-decoding acceptance on hover. From the
  engines' own histograms, and from the gateway's for llama.cpp, which keeps
  none.

**The cluster as one machine.** Nodes sharing a `cluster:` name pool their
memory, so free space is summed within a cluster and never across one — a
fleet-wide total would describe space no single model could use.
Tensor-parallel workers show as shards of one job, and two alerts compare
cluster members against each other to catch the straggler.

**The fabric.** Every interface and RoCE port: link state, negotiated rate,
throughput, errors and drops. Unplug a port and you can exclude it from
alerting by name; the RoCE device behind it goes with it, since one cable
carries both. A ConnectX-7 that negotiates 10 Gb/sec instead of 200 is
otherwise invisible.

**Every sensor, not just the GPU.** A GB10 exposes 18–23 temperature sensors —
seven SoC zones, the NVMe, one per ConnectX port, the radio — ranked by how
close each is to *its own* limit, which differ by twenty degrees across one
machine. On this cluster an SoC zone hit 95.4°C while the GPU read 72°C.
The card's **groups** menu hides the groups you don't need to read. Alerts
still watch every sensor, and the headline still names the hottest and the
closest to its limit across all of them. Each group can also show one reading
per node instead of every sensor: its **hottest**, or its **avg**, so the
seven SoC zones average into one figure for how warm a node runs, and four NIC
chips become one line. An average never hides a hot sensor: its warning and
critical colours still come from whichever sensor is closest to its limit.
Switch the card to **gauges** for a dial per node in each group.

**Fans, where the firmware declares them.** Each fan's speed shows on the node
card as `fan 2147/2183 rpm`, and **Fan speed** charts the fastest per node.
The NVIDIA DGX Spark reports two fans from BIOS 5.36_0ACUM028; the ASUS GX10
reports none at any OS version, so the reading and the line are left out there.

**Alerting you can trust.** 35 rules, with push notifications via
[ntfy](https://ntfy.sh) — no account, no API key — and to **Slack, Discord and
Telegram**, all set up from **Settings → Notifications**: ntfy's topic made and
shown to subscribe, each channel choosing every alert or critical only and
whether recoveries and reports come too, each with a test button. Thermal bands come from each
node's own hardware limits, and you're told if a node falls back to a guess. A
GX10 sits at ~84°C doing routine work, so a generic 80°C threshold would page
you all day.

**History.** 20 metrics charted over 1h to 7d, a chart per metric and a line
per node — GPU and CPU utilization, clocks, power, memory, every temperature
group and its headroom, all four PSI signals, swap and disk I/O, throughput and
prefill. Each axis fits its data but never spans less than a set minimum, so a
45–70°C climb fills the chart and memory holding at 93% stays a flat line.
**Full** puts the fixed 0–100% and 0–100°C axes back.

**Or as gauges, live.** Switch System Activity to **gauges** and each metric
becomes a dial per node, updating every second or two from the live feed
rather than Prometheus's 15-second scrape. GPU and package temperature dials
mark the node's own warning and critical limits, and past one the whole dial
turns amber or red and says so. Rates with no ceiling, like throughput, are
plain numbers. Keep a copy of the card on each view: trends in one, the
present in the other.

**Network traffic, a chart per group.** RoCE, management, Wi-Fi and the rest
each get one chart with two lines per node in the node's colour, received
solid and sent dashed, both rising from zero. The links span six orders of magnitude, so a **log** switch keeps a
12 Mb/s management spike from flattening the 100 kb/s lines beside it. Errors,
drops and an RDMA port changing state get a chart of their own when they
happen. A **gauges** view shows each node's traffic in each group now, as a
share of the rate its links negotiated. The table lists every interface, worst first, and opens any one's own
chart; past a dozen links the card opens as the table.

**Optionally, NVIDIA release updates.** The dashboard can check every Spark
against NVIDIA's release recipes and install what's available: an *updates*
button in the header, and a panel showing what each update would change before
you start it. It runs NVIDIA's own sequence — `apt full-upgrade`,
`fwupdmgr upgrade`, restart — one Spark at a time over SSH.

**It's off until you set it up**, and that takes an SSH key and a login on each
Spark: [the quick setup](fleet-updates-setup.md). Nothing installs on a
timer — the hourly part is a read-only check, and every update is a button
somebody pressed.

![DGX OS updates open over the dashboard: the pooled pair sparketa and sparkjr shown together with the two 200 GbE links between them, each with routine and security Ubuntu updates waiting, and the standalone sparky with its own updates and the record of its last one, the July 2026 release with 204 package updates applied](../Screenshot-dgx-os-updates.jpg)

DGX OS updates on three machines, with `sparkjr` expanded to show what the
release would change. The button says *update sparkjr and sparketa* because
those two pool memory: updating one without the other breaks the pair. *6
packages pinned here* means someone ran `apt-mark hold` on that node, so the
count isn't a misleading zero.

**Optionally, who is calling.** The engines count requests and never record the
caller. Put a [LiteLLM](https://docs.litellm.ai/docs/simple_proxy) proxy in
front of them and a Clients card shows which harness, on which machine, sent
how many requests to which model on which node, how many failed, and the
tokens in and out. An agent that sends an SDK's default User-Agent is still
named, by its machine.

**Optionally, ask it.** Connect any model you run, or one behind the gateway,
and an **ask** button takes questions about the cluster's history: "why was
sparky slow at 9pm?", "which node ran hottest?", "who has been using the
cluster most?". It answers from what the dashboard already holds for the
window you pick: alerts, events, how each chart moved and when it peaked, and
who called. Asked which node ran hottest, GLM-5.3-Flash named sparkjr's
96.4°C at 15:21 and the alert that caught it, in under six seconds. A model
that can use tools may also look further on its own: asked why sparky had been
slow, Qwen 3.6 35B checked what moved around 15:44 and named swap thrashing,
with the memory pressure and swap traffic behind it. The model can read about
the cluster and can't act on it.

**Reports, on a schedule or on demand.** Daily, weekly, monthly or every few
hours, a report of the period: each node's load, temperature and memory, what
used the GPU, model loads and latency, alerts, the network, gaps in the data,
and what moved against the period before. Every figure is computed, so a
report is right with no model involved; the assistant can add a short summary
on top, marked as its own. Kept on a **Reports** page, as a PDF (A4,
portrait or landscape, previewed before you download it, no print dialog
involved), or as Markdown, HTML, CSV or JSON. Or pick the period, the nodes and
the sections and build one on the spot. A notification says when a scheduled
one is ready, with its headline figures and a link.

**Or let your own agent look.** The same read-only tools are offered to agents
over MCP, so Claude Code, Hermes, Codex, Cursor or any agent that speaks MCP
can query the dashboard directly: the cluster now and how it is put together,
any chart's series over any window, what moved around a given moment, this week
against last, alerts and the rules behind them, model loads and latency, who
called, and whether the monitoring itself had gaps. It can say what was using
the GPU at any past time, by workload, not just now: asked what sparky was
doing one afternoon, an agent found ComfyUI at 23% of the GPU, against 26% for
the whole node. Ready-made prompts give a daily report, an incident report or a
capacity check in the same shape from any agent, and agents can be allowed to
note things on the timeline ("benchmark started"). Off until you switch it on
in Settings, which makes the key and the setup for your agent
([setup](deployment.md#tools-for-agents-mcp--optional)).

## The page is yours

Move, size, copy and put away any card. It's all remembered in the browser —
nothing is configured on the server.

- **Move it.** Drag by the grip in the left gutter, or use the arrow keys. Drop
  a card beside another to pair them side by side.
- **Size it.** Drag the bottom-right corner. Height moves one table row at a
  time so cards stay on a grid, and you can drag *past* the content to make two
  columns end level. Sideways flips between half and full width.
- **Too short for its content?** It pages. Or pick **Scroll** in **Settings →
  Appearance** and cards scroll inside their box instead, headers staying put.
  A chart card held taller than its charts centres them, and shrinking it takes
  up the empty space before the charts.
- **More than one.** `+ element` adds another copy of any card — one Network
  Activity showing RDMA ports, another showing charts.
- **Each card has its own menus.** Which metrics, which interface or sensor
  groups, fitted or full axes, linear or log, charts or table or RDMA ports,
  which columns. Column edges drag, and a faint line between headers marks each
  one.
- **Compact node cards.** **Settings → Appearance → Node elements → Compact** puts every node
  on one grid at the same width, each cluster and each standalone node in its
  own frame. When the frames fit in half the page, as on a wide monitor, the
  right half becomes a column like the ones below: drag a half-width card into
  it. A single row of frames spreads to fill its half, and stretches to end
  level with a taller card beside it.
- **Three layouts to start from.** **Side by side** (the default, as in
  the screenshot above), **Stacked** (every card full width) and **Wall
  display** (gauges only, for a screen across the room). Pick one in the
  setup guide or **Settings → Appearance → Layout**; anything you change
  afterwards is kept on top of it.
- **Name it.** **Settings → Appearance → Title** replaces spark-dash in the
  header and the browser tab.
- **Nine themes, each light and dark**: Default, Catppuccin, Solarized,
  Darcula, Cyberpunk, Forest, Slate, Paper and High contrast. Pick the
  theme and Auto / Dark / Light in **Settings → Appearance**; the sun or
  moon in the header flips light and dark.
- **Card headers.** **Settings → Appearance → Card headers** puts a band
  behind each card's title: Raised, Tinted (the default), Solid, or Off.
- **Node chips on the Activity cards** hide or show a node on every chart
  (System Activity; shift-click shows only that node). On a cluster too big
  for the chips to fit beside the controls they fold into a **nodes** menu.
  **Settings → Appearance → Reset layout** puts it back to the layout you
  picked.
- **Settings** opens as a wide panel with a section on each tab of its left
  rail, each integration's state shown beside its name; drag the panel's left
  edge to make it wider or narrower.
