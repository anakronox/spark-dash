# Deployment

## Principle: Docker-only, base OS stays untouched

Everything in this stack ships as Docker containers, deployed via Compose. The
only things allowed to exist outside a container are:

1. **Docker Engine** itself.
2. **NVIDIA Container Toolkit** (`nvidia-ctk` / the `nvidia` container
   runtime) — already installed and configured today, since it's required for
   the existing dockerized llama.cpp/vLLM containers to get GPU access. Not
   new scope introduced by this project.

Nothing else gets `apt install`-ed, no systemd units get hand-written, no
kernel modules get built, on any GX10 node. If a piece of the
monitoring stack can't be satisfied that way, it gets descoped rather than
quietly becoming a host change — see the `spark_hwmon` decision below for a
concrete example of that tradeoff actually being made.

## Why this matters here specifically

DGX OS on the GX10 is comparatively hard to reinstall/reimage cleanly compared
to a generic Linux box, and it's shared with the actual inferencing workload —
the whole point of the hardware. Keeping every monitoring component
containerized and disposable means:

- A bad exporter config is `docker compose down`, not a broken host package.
- Adding a node is "copy a directory and set one value," not "re-run a
  multi-step install script and hope it's idempotent."
- Nothing about the dashboard risks destabilizing the host OS the inference
  workloads depend on.

## Per-node services (identical Compose stack on all 3 GX10s)

**Exporters must run on each Spark — this is unavoidable.** `/proc/meminfo`,
`/proc/pressure/memory`, NVML, and per-process GPU memory are all
machine-local; a Prometheus running elsewhere cannot read another host's
`/proc`. That's the standard Prometheus split — collection is local, storage
and query are central. What *is* avoidable is bloat: it's two small sidecar
containers per node, not a monitoring stack per node.

| Service | Image source | Host access needed |
|---|---|---|
| `node-exporter` | official `prom/node-exporter` | read-only bind mounts: `/proc`, `/sys`, `/` |
| `spark-dash-agent` | ours, custom | `--gpus all` (NVML/`nvidia-smi` via Container Toolkit); read-only bind mounts `/proc`, `/sys`; network access to the local llama.cpp router |

vLLM itself needs no sidecar — Prometheus scrapes its native `/metrics`
directly; it's already a container on the existing inference stack.

### Why one agent instead of separate exporters

Earlier drafts split our custom collection into `gb10-node-exporter` and
`llama-router-exporter`, alongside a third-party GPU baseline exporter. That's
over-decomposed for this scale. Both custom pieces are ours, both Python, both
just poll local sources and expose `/metrics` — so they ship as **one
`spark-dash-agent` image** with internal collector modules:

- `gpu` — NVML via `nvitop`: utilization, temperature, power, clocks,
  per-process GPU memory
- `memory` — UMA-correct calc (`vm.total - vm.available`)
- `psi` — `/proc/pressure/memory`
- `clock` — load-gated throttle state machine
- `llama_router` — llama.cpp router-mode aggregation (reports nothing on a node
  not running llama.cpp, so the same image works everywhere)

One image to build and version, one config, one scrape target per node.
Collectors stay separate modules internally, so splitting them back out later
is cheap if there's ever a reason.

### GPU baseline exporter — will not ship (decided 2026-08-21)

`dcgm-exporter` / `dgx-spark-prometheus` are **not shipped, and are not
planned.** This was deferred through three phases; closing it as a decision is
more useful than carrying it as a maybe.

The agent already reads NVML directly for utilization, temperature, power and
clocks, and `dcgm-exporter`'s headline advantage — per-GPU memory accounting —
is exactly the number unified memory breaks on GB10. Adopting a daemon for its
weakest feature here was never the trade.

**What it would genuinely add is the one blind spot that remains:** memory
bandwidth. `nvmlDeviceGetUtilizationRates().memory` reads 0% while the GPU is
at 96% (measured 2026-08-16), and on a unified-memory part bandwidth is
plausibly the real bottleneck. DCGM's profiling counters
(`DCGM_FI_PROF_DRAM_ACTIVE`) are the only remaining route to it.

**Three things make that not worth it here:**

- **It is not installed, and would have to be.** Checked on a GB10 2026-08-21:
  no `dcgmi`, no `nv-hostengine`, no `libdcgm`, on driver 580.173.02. This is a
  resident daemon on every node plus an exporter against it — not a container
  to add.
- **It spends the node budget on monitoring.** The node stack is deliberately
  two containers with no persistent state, measured at ~78 MiB, precisely so
  the box stays free for models. DCGM is not free, and profiling counters carry
  overhead on the GPU itself.
- **Whether GB10 exposes the counters at all is unknown**, so the work starts
  with an experiment whose answer might be "no".

**If that blind spot ever becomes a real question**, the spike is one node and
one command — install DCGM on a single box and run `dcgmi dmon -e` against the
profiling field IDs to see whether DRAM activity is reported. Answering "no"
would be a real result worth recording. Until something is actually
unexplained, adding a daemon to chase a number nobody has needed is the wrong
direction for a stack whose whole premise is costing the inference node as
little as possible.

If either is ever revisited, note that `dgx-spark-prometheus` upstream ships a
systemd/host-binary install path (`go build`, `sudo cp` to `/usr/local/bin`, a
`.service` file) which conflicts with the Docker-only rule. It's a
self-contained static Go binary reading `/proc`, `/sys`, and `nvidia-smi`, so
wrapping it in our own multi-stage Dockerfile is straightforward.

**Networking:** the agent needs to reach the existing llama.cpp/vLLM
containers (for its `llama_router` collector, and for Prometheus to scrape
vLLM's `/metrics`). Since those already run via their own `docker compose` stacks per
host, our monitoring stack either joins the same Docker network (declared as
`external` in our Compose file) or reaches them via ports already published to
the host. Prefer joining the existing network where possible — avoids relying
on published ports that might change.

## Footprint on the inference node is a design constraint

The GB10 is an inference workhorse. Monitoring it should cost it as close to
nothing as possible, which is why the per-node stack is two containers with no
persistent state and no data directory, and why everything that stores or
serves anything lives elsewhere.

Measured 2026-08-16 on `sparky`: agent **91 MiB**, node-exporter **10 MiB** —
about 0.08% of a 121 GiB unified pool. On GB10 that matters more than the
percentage suggests, because there is no separate VRAM: every byte the
monitoring stack holds is a byte a model cannot. See
[metrics.md](metrics.md) and the unified-memory notes.

Most other Spark dashboards run directly on the Spark. This one defaults to
not doing that. Running everything on one GB10 **is supported** — see
[Single-host](#single-host--everything-on-one-gb10) below — but it is opt-in,
with its cost stated rather than discovered.

## Central stack — a dedicated Proxmox VM (settled)

**Decision: by default the central stack runs on its own Docker host — here, a
dedicated VM on the existing Proxmox cluster — rather than on a GX10.** This is
the recommended shape and the one this deployment runs. It is not the only
supported one: [Single-host](#single-host--everything-on-one-gb10) below puts
everything on one GB10 and states what that costs.

### Why not on a GX10

The argument is failure domains, not resources. It is an argument for a
default, not a prohibition — [Single-host](#single-host--everything-on-one-gb10)
below accepts every point here deliberately, for the one reader who has no
second machine:

- **"Node down" is one of the primary alerts.** If Prometheus lives on node 1,
  a node-1 crash takes out both the node *and* the history explaining why it
  went down — you're blind at exactly the moment the data matters most. A
  monitor sharing a failure domain with what it monitors is worth much less.
- **Symmetry.** Hosting the central stack on node 1 makes it non-interchangeable
  with nodes 2 and 3, and puts an asterisk on Phase 2's "copy the Compose file
  to the new node."
- **Attack surface.** `cloudflared` belongs wherever the dashboard runs. Keeping
  it off the GX10s means they never host an externally-reachable service.
- Resource contention is the *weakest* argument — Prometheus at this scale is
  genuinely small — so it's listed last on purpose.

The correct failure mode falls out of this: if the monitoring VM dies, you lose
visibility but inference keeps running; if a GX10 dies, you still have the
history. Neither failure takes the other down.

### Single-host — everything on one GB10

**Supported, opt-in, and the trade is stated here rather than discovered.**
Not everyone has a spare machine, and "you need a second box" is a worse answer
than "here is what it costs".

```bash
cd central
cp cluster.yml.single-host.example cluster/cluster.yml
$EDITOR cluster/cluster.yml     # one edit: this machine's LAN IP
docker compose -f compose.yaml -f compose.single-host.yaml up -d
```

**What you give up is the section above.** Everything in *Why not on a GX10*
still applies — it is not wrong on one box, it is accepted:

- **The failure domain collapses.** "Node down" is a primary alert, and now the
  box that goes down is the box holding the record of why. You keep the alert
  and lose the post-mortem, which is exactly backwards from what you want at
  the moment it matters.
- **Nothing changes about attack surface** if you do not publish the dashboard.
  If you do, `cloudflared` now runs on the inference node.
- **Resource contention remains the weakest objection**, and now it is
  measured. See below.

**What it costs, measured 2026-08-21 on a running three-node install:**

| | memory |
|---|---|
| node stack | ~78 MiB (agent 69, node-exporter 9) |
| central stack | ~178 MiB (backend 67, Prometheus 84, Alertmanager 19, exporter 8) |
| single-host total | **~220 MiB** — one node's worth of series, minus the dropped exporter |

On a 121.7 GiB unified pool that is **~0.18%**. The TSDB was 285 MB for 9,205
series across three nodes; one node is roughly a third of that. Negligible
against a model — but on GB10 it comes out of the *same pool the models use*,
which is why it is a number here rather than a shrug.

**What the overlay actually changes**, and nothing else:

- **Drops the central `node-exporter`.** Not for the 8 MiB. That exporter is
  scraped as `node-exporter-central`, a job which hardcodes `node: sparkmon` —
  on one box that invents a node that does not exist, roughly 640 series
  describing the GB10 under another machine's name, in the same label space as
  the real one. Every `by (node)` query would gain a phantom member.
- **Points Prometheus at `prometheus.single-host.yml`**, which is that same
  file minus that one job. `alerts.yml` is shared, not copied.
- **Swaps one alert rule.** `PrometheusStorageFillingUp` pins itself to the
  host holding the TSDB by job name, and that host is different here. The
  single-host variant drops the pin because with one host there is exactly one
  root filesystem to compare against.

**The two files are not interchangeable, and getting it wrong is loud.** Using
the single-host storage rule on a multi-host install produces
`found duplicate series for the match group {}` and the rule stops evaluating —
verified against a live three-node instance. `tests/test_single_host.py` checks
the variants stay in step.

**The one thing that will stop you: `host:` cannot be `localhost`.** The
backend resolves it from inside a container, so `localhost` is the container.
Everything starts cleanly and the dashboard reports the machine you are sitting
at as unreachable, blaming the agent. Use the LAN IP —
`ip route get 1 | awk '{print $7; exit}'`. The same applies to `BACKEND_URL` in
the node stack's `.env`.

### Services

| Service | Image | Notes |
|---|---|---|
| `prometheus` | official `prom/prometheus` | TSDB + scraping. Stock image, own lifecycle. |
| `alertmanager` | official `prom/alertmanager` | Routes alerts to ntfy; reads the topic from `secrets/ntfy-url` on every send. |
| `spark-dash-backend` | ours | FastAPI; also serves the built Svelte assets, so there's no separate web server. Also serves the MCP endpoint at `/mcp`. See [app-design.md](app-design.md). |
| `node-exporter` | official `prom/node-exporter` | The monitoring host's own disk, CPU and memory. |
| `cloudflared` | already running | Reuse the existing tunnel — add a hostname/route + Access policy rather than standing up a second connector. |

Two new containers, plus a route on infrastructure that already exists.

### Getting started — the setup guide

The first time the dashboard opens, **Get started** lists what to set up:
*required* (every node reporting, Prometheus scraping with no target down, an
alert route with a channel), *recommended* (thermal limits read from each
node's hardware, nodes kept in `cluster.yml`, pooled nodes named as a cluster),
then the *optional* integrations. `GET /api/setup` works each step out from
the system itself, so a step reads done only when the thing works. Each step
opens Settings on its pane. While a required step is missing, a **setup** chip
in the header says how many. A short tour of the controls that are easy to miss
follows the first time; both reopen from **Settings → Appearance**, and **Reset
layout** does not bring them back.

### Reports — scheduled and on demand

A report is a period, a set of nodes and the sections chosen, in order:
overview (per node: GPU use, temperature, memory, power, throughput, average
and peak with when), what used the GPU (per workload), models (loads and how
long, decode throughput, time to first token), alerts, events, temperatures
(the peak per group and the least headroom), network (per interface: traffic,
errors, drops), clients, monitoring health (gaps, so a report says where its
own numbers have holes), DGX OS updates, and a comparison with the period
before. The figures are computed from Prometheus, never generated; with the
assistant set up, a schedule or a built report can add a three-to-five
sentence summary on top, marked as the model's. A section that cannot be
built says so and the rest stand.

**Schedules** live in **Settings → Schedules** and in `cluster.yml` as a
`reports:` block: daily at a time, weekly on a day, monthly on a date (past a
short month's end, its last day), or every N hours on whole hours from
midnight. Each covers the period since the run before. Times are wall-clock
in the configured zone, so "daily at 08:00" stays 08:00 across a change to or
from summer time; the zone defaults to the monitoring host's, which the
backend reads from the host's `/etc/localtime` (mounted read-only; it matches
the file's contents to a zone name), and the dropdown overrides it. A new
schedule does not fire at once; a run missed while the backend was down runs
once on start, for the latest period. **run now** builds one immediately.

**Kept** under `cluster/reports/` as JSON, the newest 200 and none older than
180 days by default. The **reports** page in the header lists them; each can
be printed (a self-contained HTML page opens and asks to print, which is the
PDF) or downloaded as Markdown, HTML, a zip with one CSV per table, or JSON.
**build** makes one now from a preset or a from-to period (at most 31 days),
the nodes and the sections, kept only when asked.

**Notified** through ntfy when a scheduled report is ready, if the schedule
says so: the title, its headline figures, and a link to the report, which
needs **address** set to how you reach the dashboard. Slack, Telegram and
Discord follow with roadmap BJ.

Agents read and build reports too: `list_reports`, `get_report` and
`build_report` over MCP.

### Notifications — ntfy

Alertmanager sends every alert, and every recovery, to an
[ntfy](https://ntfy.sh) topic by its webhook. The topic's URL lives in
`secrets/ntfy-url`, out of git, because on ntfy.sh the topic name is the only
access control. **Settings → Notifications** shows where alerts go (read from
Alertmanager's loaded config), the topic masked until you show it, how to
subscribe, **Send a test** (shaped like a real alert, so it renders through
ntfy's alert template), and **New topic** (random; two presses when one exists,
since phones on the old topic stop receiving), or your own topic and server.

Alertmanager reads the file on every send, so a change applies to the next alert
with no restart. For the backend to write it, the compose file mounts `secrets/`
into it at `/etc/spark-dash-secrets` and adds Alertmanager's group
(`group_add: ${PGID:-65534}`), and the directory is setgid and group-writable:

```bash
sudo chmod 2770 secrets && sudo chmod 660 secrets/ntfy-url
```

Until then the pane names the missing piece. A hand-maintained compose copy (a
deploy tool running compose from its own directory) needs the same mount and
`group_add`. Setting the topic by hand, and self-hosting ntfy, are in
[central/README.md](../central/README.md#notifications-ntfy).

### DGX OS updates — optional

**This is optional, and off by default.** The dashboard is complete without
it: skip this section and there is no extra mount, no SSH key and no button,
and nothing else in the stack behaves differently.

**Start here: [DGX OS updates: the quick setup](fleet-updates-setup.md)** —
make a key, put it on each Spark, add one line to `.env`, redeploy. The
updater runs inside the backend; there is no second container.

The capability is a compose overlay rather than a profile, so an install that
never opts in carries none of it:

```
COMPOSE_FILE=compose.yaml:compose.fleet.yaml
SPARK_FLEET_SSH_USER=youruser
```

`compose.fleet.yaml` mounts `central/fleet-ssh` (the key, read-only) and
`central/fleet-state` (the enrolment list, posture, run records and the
`known_hosts` ssh pins into). **Both must be owned by uid 10002**, the backend
image's user, or ssh will not read the key — Docker will not do this for a
bind mount, and it auto-creates a missing source as an empty root-owned
directory, which fails quietly.

If a deploy tool runs its own copy of `compose.yaml` from elsewhere, that copy
needs the same four environment lines and two mounts, with the mounts made
absolute. Missing them is silent: the backend comes up healthy and Settings
says the feature is not set up.

The password rule is not bypassed: through the tunnel (`https://`, where
cloudflared sets `X-Forwarded-Proto`) an update accepts the sudo password; on
the plain-HTTP LAN it is refused. `SPARK_FLEET_ALLOW_PLAIN_PASSWORD=1` is the
opt-out and yours to set. Sparks that grant passwordless sudo never ask.

`/health` reports `fleet_updates: ok | unreachable | not configured`, and
names any Spark that has stopped refreshing its package lists
(`updates_not_refreshing`) or was left with its own DGX Dashboard updater
paused (`dashboards_paused`). Unreachable is not counted as a problem — the
dashboard is not blind without this.

**Until 2026-09-23 this ran as a separate `spark-fleet-updates` container**
that the backend proxied to, behind a `fleet` compose profile. It was folded
into the backend ([roadmap AL](roadmap.md#al--the-fleet-updater-folded-in--branch-fleet-embedded-started-2026-09-22))
and the proxy was removed once a real update had run through the new path. If
you are upgrading from that layout: drop `COMPOSE_PROFILES=fleet` and
`FLEET_UPDATES_URL` from `.env`, add the two lines above, and copy the old
`fleet/data/fleet.json` to `central/fleet-state/` — your enrolment list is
migrated in place, so nothing needs re-ticking.

### Asking the dashboard — optional

**Off until you give it a model.** In Settings → Assistant: an
OpenAI-compatible address (`http://host:4000/v1`; a bare `host:port` gets
`/v1`), an API key if the endpoint needs one, **Detect** to list its models,
**Test** to send one tiny request and see the reply and how long it took, and
Save. An **ask** button appears in the header.

**What a question sends.** The question, and as text: every node as it stands
(health, GPU, memory, CPU, pressure, its largest GPU processes, the models
loaded), the alerts firing now, the alert episodes and events in the chosen
window (1h, 6h or 24h), time to first token per model, min/avg/max with the
time of the peak for ten series per node, and, with client stats on, the
Clients card's rows for the window: who called which model on which engine,
how often, how many failed, and their tokens. About 3.5 KB for this
three-node cluster over an hour.

**Tools.** On by default: the model may also call the read-only tools under
[Tools for agents](#tools-for-agents-mcp--optional), for up to six rounds, then
answers; the answer lists what it looked up. A model that does not call tools
just answers, and an endpoint that refuses them (llama.cpp without `--jinja`)
is asked again without. Switch it off in Settings to send the bundle alone.
Measured 2026-10-16: Qwen 3.6 35B on llama.cpp took 2 to 4 seconds a round
once loaded, and answered "why was sparky slow" in 61 seconds after one
`correlate` call. Nothing acts on what the model says.

**Where it is kept.** The address, the model and the switch are an
`assistant:` block in `cluster.yml`. That file is served to the browser, so
the key is not in it: it is `assistant.key` beside it, owner-only, and
Settings only ever shows whether one is set and its last four characters.
Saving with the key field empty keeps the stored key; **Remove** deletes both.
Its requests carry the User-Agent `spark-dash-assistant/<version>`, so through
the gateway they show on the Clients card as "spark-dash assistant".

A reasoning model can take a while: on this cluster GLM-5.3-Flash answered a
one-hour question in 6 to 24 seconds. The backend waits up to 90 seconds for a
question without tools, and 180 with them. A question with tools is streamed,
a space every ten seconds and then the answer, so a proxy that gives up on a
silent origin (Cloudflare's tunnel, at 100 seconds) does not cut it off.
Measured 2026-10-16 with ComfyUI holding sparky's GPU at 96%: Qwen 3.6 35B
took 120 seconds over one `correlate` call.

### Tools for agents (MCP) — optional

**Off until switched on** in Settings → Agent tools. Switching it on
makes a key and shows it once. Settings shows the endpoint and the key on
their own, and an **agent** picker writes the command or config block for
Claude Code, Hermes Agent, Codex CLI, Gemini CLI, Cursor, VS Code or OpenCode;
**Other** gives the address and header for anything else that speaks MCP over
streamable HTTP. Every one is the same two things, the address and an
`Authorization: Bearer <key>` header. For Hermes, in `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  spark-dash:
    url: "http://<monitoring-host>:8080/mcp"
    headers:
      Authorization: "Bearer <key>"
```

then `/reload-mcp` in a session. For Claude Code:

```sh
claude mcp add --transport http spark-dash http://<monitoring-host>:8080/mcp \
  --header "Authorization: Bearer <key>"
```

**new key** replaces the key; the old one stops working at once.

**The tools.** All read-only, and each is a read the page already makes:

| tool | answers |
|---|---|
| `cluster_now` | each node now: health, GPU, memory, CPU, pressure, temperature bands, GPU processes, engines and models; alerts firing |
| `list_metrics` | the named series, with unit and meaning |
| `metric_history` | one series over a window, per node: min, avg, max and when, first and last; the values on request, at most 240. `gpu_workload_sm` and `gpu_workload_memory_bytes` are per workload (runtime and model), so they say what was using the GPU at a past time |
| `alerts`, `events` | alert episodes; deploys, model loads, cold starts, maintenance |
| `latency` | time to first token per model: the last 15 minutes, or any window, with when each peaked |
| `compare` | two windows of equal length side by side, every series per node ranked by how far it moved |
| `monitoring_health` | each scrape target's gaps in a window and state now, agent versions, data freshness, targets gone a day or more |
| `topology` | which nodes pool into a cluster, the routers and engines on each, what each serves now |
| `alerting` | the alert rules with their expressions and `for:`, which are firing, active silences and maintenance windows |
| `model_activity` | per model: loads with how long each took, decode and prefill throughput, time to first token |
| `updates` | DGX OS release, pending packages and firmware per node, when the updater is set up |
| `clients` | who called through the gateway, when client stats are on |
| `correlate` | every series on every node ranked by how far it moved around a moment, with the alerts and events beside it |
| `promql` | raw PromQL, only with **also allow raw PromQL queries** on |
| `list_reports`, `get_report`, `build_report` | kept reports, one as Markdown or JSON, or a new one for any period, nodes and sections |
| `add_note` | adds a note to the timeline ("benchmark started"), marked as an agent's; only with **let agents add notes** on. The one tool that writes. Settings shows how many there are and removes them all |

The endpoint also serves **prompts**, report templates any client can offer
(`daily_report`, `incident_report`, `capacity_check`), and **resources**, reference an
agent can read instead of guessing: `sparkdash://guide/gb10` (unified memory,
temperature limits, clocks, power), `sparkdash://guide/metrics` (every series, its
unit and its PromQL) and `sparkdash://guide/investigating` (which tool answers what).

Series are picked by name, as the charts pick them, so a call cannot become
an arbitrary Prometheus query unless raw PromQL is switched on. Results are
capped (40 series, 60 rows, 240 points) and say when they were cut. Times are
UTC.

**Where it is kept.** The two switches are an `mcp:` block in `cluster.yml`;
the key is `mcp.key` beside it, owner-only, as the assistant's is.

**Behind Cloudflare Access**, an agent cannot sign in with Google; give it a
[service token](https://developers.cloudflare.com/cloudflare-one/identity/service-tokens/)
and add its two headers to the client, or connect on the LAN address.

### Client stats — optional

**This is optional, and off until you give it a gateway.** It shows which
clients and harnesses are calling the engines, from which machines, how many
requests each sent to which model, and how many failed.

**It needs a LiteLLM proxy in front of the engines.** llama.cpp, vLLM,
SGLang and TensorFold count requests per model and never record who sent them. The only
place a client names itself is its HTTP request, so the one way to see
callers is a proxy that every client talks to. LiteLLM's Prometheus metrics
carry each request's User-Agent, client IP and the engine it went to, and
that is what the dashboard reads.

Running the gateway is outside this project. LiteLLM's own docs cover it:
[the proxy](https://docs.litellm.ai/docs/simple_proxy),
[deploying it](https://docs.litellm.ai/docs/proxy/deploy),
[its config](https://docs.litellm.ai/docs/proxy/configs),
[Prometheus metrics](https://docs.litellm.ai/docs/proxy/prometheus),
[health checks](https://docs.litellm.ai/docs/proxy/health) and
[the source](https://github.com/BerriAI/litellm). What the dashboard needs
from it:

- the Prometheus callback on;
- `/metrics` readable without a key;
- clients pointed at the gateway rather than at the engines. A client that
  still calls an engine directly does not appear.

The engines are scraped directly either way. The gateway adds who called; it
replaces nothing. It also gives llama.cpp models a time to first token in the
header: llama.cpp keeps no latency histogram, so without the gateway only
TensorFold and vLLM models have one.

**Naming programs that send an SDK's User-Agent.** Many agents send their
library's default User-Agent unchanged, so the card can say "OpenAI SDK
(Python)" but not which program. A gateway can do better from the request
itself, its headers and tool names. If it publishes two counters,
`gateway_harness_requests_total{harness, user_agent, client_ip,
requested_model, api_base}` and `gateway_harness_failures_total{harness,
user_agent, client_ip, requested_model, exception_status, route}`, counting
the same events as LiteLLM's `litellm_deployment_total_requests_total` and
`litellm_proxy_failed_requests_metric_total`, the card counts from those
instead, one row per harness. If it also publishes
`gateway_harness_tokens_total` (the same labels plus `direction`, `in` or
`out`), the card's **tokens in** and **tokens out** columns fill in; LiteLLM's
own token counters are labelled by API key, not by client, so without it they
read "—". That counter starts with the gateway process, so for a window
reaching back past a gateway restart the header says *tokens since* the
restart, where the request counts carry on across it. It knows `hermes` (Hermes Agent), `hermes-aux`
(its side calls, tagged and marked inferred), `opencode`, `open-webui` and
`spark-dash-assistant`,
and shows any other key as itself. An empty `harness` falls back to the
User-Agent. If the counters started inside the card's window, the card says
when it began counting. Without them, nothing changes.

**Turning it on.** In Settings, under Clients: paste the gateway's address
(the `…/v1` URL clients use is accepted), press Test, save, and switch it on.
That writes a `gateway:` block to `cluster.yml`, so it needs one: a
deployment whose nodes still come from `SPARK_NODES` is told to move them
first. Prometheus starts scraping the gateway within 30 seconds and the
Clients card appears. Switching it off stops the scrape and removes the
card, and the history already collected is kept. The block is documented,
commented out, in `central/cluster.yml.example`.

Prometheus reads the gateway through a `litellm` job in
`central/config/prometheus.yml`, with a target list the backend writes and
leaves empty while the feature is off. An existing install picks the job up
with the usual config pull and reload.

**What the dashboard sends the gateway.** Test requests `/health/liveliness`
and `/metrics`, and nothing else. After that only Prometheus talks to it,
and only to read `/metrics`.

One caution about the gateway itself: LiteLLM's health checks (its `/health`
route, background health checks, and the check buttons in its admin UI) send
a real request to every configured model. Behind a llama.cpp router started
with `--models-autoload`, that loads each one. See
[its health-check docs](https://docs.litellm.ai/docs/proxy/health).

`/health` reports `litellm: ok | not scraped | off | not configured |
unknown | invalid: <why>`. None of these counts as a problem: the dashboard
is not blind without a gateway.

| Settings or `/health` says | Meaning |
|---|---|
| No answer from … | The gateway is not reachable from the monitoring VM at that address. |
| … answered 404 on /health/liveliness | Wrong port, or not a LiteLLM proxy. |
| The gateway asks for a key on /metrics | LiteLLM requires a key there by default. Its [Prometheus docs](https://docs.litellm.ai/docs/proxy/prometheus) cover turning that off. |
| /metrics … carries no LiteLLM series | The Prometheus callback is not on. |
| `not scraped` | Prometheus cannot reach the gateway, or has not refreshed its targets yet (30 s). `up{job="litellm"}` says which. |
| … configured in the cluster file, and this deployment does not have one yet | Nodes still come from `SPARK_NODES`. Move them into `cluster.yml` first. |
| `invalid: …` | The `gateway:` block in `cluster.yml` has a mistake, named in the message. Scraping continues at the last good address, and saving an address in Settings replaces the block. |
| Every client shows the same address | Something else proxies in front of the gateway, so it is the only client LiteLLM sees. |

### Sizing

2 vCPU / 4GB RAM / ~50GB disk is comfortable for a handful of nodes. Prometheus
storage is the only thing that grows, and it has been measured rather than
estimated: ~65 MB/day for three nodes at a 15s interval, so the shipped
`PROM_RETENTION=180d` comes to about 12 GB — comfortable on a 50 GB disk,
where 365d would take over half the free space.

Since it's a Proxmox VM, snapshots and (if the cluster is configured for it) HA
migration come for free. The TSDB itself isn't precious — all *config* lives in
this git repo, so a rebuilt VM plus `docker compose up` restores the stack;
only history is lost.

### Monitoring the monitor — existing UptimeKuma instance

A monitoring stack can't alert that it's down. That gap is covered by the
existing **UptimeKuma** instance rather than anything built here.

**Primary check:** an HTTP monitor against the backend's `/health` endpoint
(see [app-design.md](app-design.md#rest--history--inventory-prometheus-backed)).
One check covers the whole chain — VM down, Docker down, backend crashed, or
network partition all surface the same way. `/health` should be *meaningful*
rather than a bare `200 OK`: it reports degraded when Prometheus is unreachable
or the live-poller has stalled, so a wedged-but-running backend is caught too.

**Placement caveat — the same failure-domain argument applies recursively.**
UptimeKuma must not live on the monitoring VM (useless), and ideally not on the
same Proxmox host (a host failure would take out both the monitor and its
watcher). A different node in the cluster is fine; genuinely external is best.
Worth confirming when this is wired up.

**Optional bonus:** UptimeKuma can also check each GX10's agent endpoint
directly. That's redundant with Prometheus in normal operation, but it means
node-liveness still works when the monitoring VM itself is the thing that's
broken. Keep it lightweight — the goal is a second opinion on "is the node
reachable," not a second alerting system.

## `spark_hwmon` — evaluated, deliberately descoped

GB10 power-rail and `PROCHOT` telemetry (see [metrics.md](metrics.md)) would
require [`spark_hwmon`](https://github.com/antheas/spark_hwmon), which is a
real ACPI-binding Linux kernel driver: `dkms`, kernel headers, and potentially
Secure Boot MOK key enrollment. There's no containerized way around this —
kernel modules load into the host kernel regardless of whether the `dkms`
commands are run from a host shell or from a privileged container with host
mounts; either way the host's kernel and its DKMS registry are modified, and
in the MOK case, DGX OS's Secure Boot chain of trust is touched too. That
directly conflicts with "base OS stays untouched," so it's out of scope by
choice — not an oversight. Everything else `spark-dash-agent` does (UMA
memory, PSI pressure, clock-throttle detection) only needs `/proc`, `/sys`,
and `nvidia-smi`/NVML, all of which are cleanly containerizable.

## Reference `docker-compose.yml` shape (illustrative, not final)

```yaml
# per-node compose file — identical across all 3 GX10s
services:
  node-exporter:
    image: prom/node-exporter:latest
    volumes:
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
      - /:/rootfs:ro
    command:
      - '--path.procfs=/host/proc'
      - '--path.sysfs=/host/sys'
      - '--path.rootfs=/rootfs'
    networks: [inference-net]

  spark-dash-agent:
    image: <our-registry>/spark-dash-agent:<tag>
    deploy: {resources: {reservations: {devices: [{driver: nvidia, capabilities: [gpu]}]}}}
    volumes:
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
    environment:
      - NODE_ID=gx10-1                        # the only per-node difference
      - LLAMA_ROUTER_URL=http://llama-router:8080   # existing container, same network
    networks: [inference-net]

networks:
  inference-net:
    external: true  # the existing llama.cpp/vLLM compose network
```

`NODE_ID` is the only value that could differ between GX10s — everything
else is byte-identical, which is what makes the Phase 2 rollout a copy.

```yaml
# central compose file — on the dedicated Proxmox VM
services:
  prometheus:
    image: prom/prometheus:<pinned-tag>
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ./targets/:/etc/prometheus/targets/:ro   # file-based SD (Phase 2)
      - prom-data:/prometheus

  backend:
    image: <our-registry>/spark-dash-backend:<tag>
    environment:
      - PROMETHEUS_URL=http://prometheus:9090
      - NODES=gx10-1,gx10-2,gx10-3    # live-poll fan-out targets
    ports: ['8080:8080']              # cloudflared + LAN both point here

volumes:
  prom-data:
```

> The compose shapes above are the ORIGINAL SKETCH and are kept for the
> reasoning around them. The real files are `central/compose.yaml` and
> `node/compose.yaml`; read those for what actually runs.

## Building and shipping images

Settled. Images are built by hand and tagged with the commit — never built on
deploy, so a rollback is a tag change rather than a rebuild. Where they go
depends on which of the two paths below you are on: a local build stays on the
host that made it, and only the registry path pins a tag in each stack's
`.env`.

**Two scripts, because they serve two different people.**

| | who runs it |
|---|---|
| `scripts/build-images.sh` | **everyone.** Builds locally, no registry, no `docker login`, no account anywhere. This is the whole job for an install. |
| `scripts/publish-images.sh` | **the maintainer**, publishing images others pull. Builds by delegating to the above, then pushes. |

Building used to be `publish-images.sh <target> --no-push` — a script called
*publish* with a flag saying *do not publish*, which failed a first-time user
against a registry they have no account on if they forgot the flag. The split
makes the common path the default one.

Four steps for the maintainer path, in order:

```bash
# 1. On a host of the TARGET architecture, from a clone of this repo
cd "$REPO" && git pull        # $REPO = wherever you cloned this

# 2. Build and push. Prints the tag to pin.
./scripts/publish-images.sh agent       # on a GX10   (arm64)
./scripts/publish-images.sh backend     # on the VM   (amd64)

# 3. Pin the printed tag in the stack's .env
#    AGENT_IMAGE=<registry>/<owner>/spark-dash-agent:<sha>

# 4. Deploy — `docker compose up -d`, or commit the .env if a deploy tool
#    watches that repo.
```

**A published tag holds ONE architecture, and the script now refuses to change
it silently.** These images are built natively where they run, so `:latest` is
whatever was pushed last. A maintainer on an amd64 monitoring VM and a
single-host user on a GB10 want that tag to mean two different things, and
overwriting one with the other gives every puller `exec format error` at
container start — long after they followed the instructions, with a message
that names nothing useful.

`publish-images.sh` compares the build's architecture against what the tag
already holds and refuses on a mismatch, suggesting an arch-suffixed tag or
`--allow-arch-change`. Getting the check itself right mattered: a plain
`docker manifest inspect` of a single-arch image **names no architecture at
all**, so the first version silently passed everything. It uses `--verbose`.

**Single-host installs are unaffected** — they build both images locally on the
GB10, both arm64, and never touch a registry. That is why the quickstart uses
`build-images.sh`.

**Multi-arch manifest lists were considered and declined, 2026-08-21.** One tag
serving both architectures is the textbook answer, and it is the wrong trade
here:

- **Every documented install builds rather than pulls.** The registry path is
  reached only by deliberately setting `AGENT_IMAGE` / `BACKEND_IMAGE`, which
  is already an explicit choice about which image you want. Nobody arrives at a
  mismatched pull by following the instructions.
- **It would reintroduce exactly what native building removed** — buildx plus
  QEMU emulation or two coordinated builders, in exchange for a case that only
  arises off the documented path.
- **Knowing your own architecture is a reasonable thing to assume**, and where
  it is not, it is cheaper to document than to engineer around.

**The case that does exist is the maintainer's, and the guard is the answer to
it.** These stacks deploy `:latest` with `PULL_POLICY=always`. Build the
backend on a GB10 while testing single-host, push it, and the amd64 monitoring
VM pulls arm64 on its next deploy and will not start — a self-inflicted outage
with an opaque message. That is one command away, and it is the scenario
`--allow-arch-change` exists to make you type out.

So the two decisions hold each other up: **declining multi-arch is what makes
the guard necessary, and the guard is what makes declining multi-arch safe.**
Revisit only if people start pulling these images rather than building them.

**Built natively, never cross-built.** The GX10s are arm64 and the monitoring
VM is amd64. Building each image where it will run avoids QEMU and buildx
multi-arch entirely, and takes seconds rather than many minutes. The cost is
building in two places, which is free because each image only ever runs on one
of them.

**Build on ONE host per image, not on each node.** The GX10s are all arm64
and the image carries nothing node-specific — `NODE_ID` comes from the host's
hostname at runtime — so `publish-images.sh agent` runs on exactly one of them.
Two nodes each building and pushing the same tag would leave the second
overwriting the first with a **different digest under the same tag**, and nodes
would then run different bytes depending on when they pulled.

### Script options

`build-images.sh`:

| | |
|---|---|
| `--tag TAG` | override the tag (default: short git sha, `-dirty` if the tree is) |
| `--keep N` | sha-tagged images to keep locally (default 5, 0 = keep all) |
| `--print-tag` | print the tag this run would use, and exit |

`publish-images.sh` — everything above is delegated, plus:

| | |
|---|---|
| `--no-latest` | push only the tag, not `:latest` |
| `--allow-arch-change` | push even though the tag already holds another architecture |
| `REGISTRY=` / `OWNER=` | override the registry; both default to the clone's own `origin` remote |

`--no-push` is gone: not-publishing is now a different script rather than a
flag on this one. `publish-images.sh` asks `build-images.sh` for the tag with
`--print-tag` rather than deriving its own, because two scripts computing "the
same" tag independently is how you publish an image that is not the one you
just built.

Deriving the registry from `git remote origin` means a fork publishes to its
own registry with no configuration, and nobody's personal registry is baked
into a tracked file.

### Why not build on deploy

Deploy tooling can often run `docker compose --build` and build from the
Dockerfile at deploy time. Deliberately not used here, decided 2026-08-16:

- **Rollback stays cheap.** Going back is a one-line tag edit and a redeploy,
  with no rebuild — and no dependency on an old commit still building.
- **Every node runs bytes known to be identical.** Build-on-deploy means each
  host builds separately, and `agent/Dockerfile` pulls
  `ghcr.io/astral-sh/uv:latest` unpinned, so the same commit can produce
  genuinely different images on different days.
- **The version label would break.** `BUILD_VERSION` is passed as a build arg
  from the git sha; compose cannot compute one. If it defaulted to `unknown`,
  every node would report the same placeholder and `AgentBuildSkew`
  (`count(count by (build) (...)) > 1`) could never fire — coverage that looks
  real and isn't.

Revisit only if the deploy tool can supply the deployed commit as a build arg;
that removes the third objection and weakens the second.

### `BUILD_VERSION` and knowing what is actually running

The script passes `--build-arg BUILD_VERSION` to both images and both consume
it: `ARG BUILD_VERSION` becomes `ENV AGENT_VERSION` / `ENV BACKEND_VERSION`.

- The agent's feeds `sparkdash_agent_build_info` and the `AgentBuildSkew`
  alert.
- The backend's is reported by `/health` as `backend_version`.

**`AgentBuildSkew` alone is not enough**, which is why the backend reports its
own. That rule is `count(count by (build) (...)) > 1` — it compares nodes
against EACH OTHER. With one node it is always 1 and can never fire, and it
can never see a backend and an agent that have drifted apart at all. That is
exactly how an agent sat six commits behind unnoticed on 2026-08-16. Comparing
`backend_version` against `agent_versions` in a single `curl /health` is the
check that catches it:

```bash
curl -s localhost:8080/health | jq '{backend_version, agent_versions}'
```
