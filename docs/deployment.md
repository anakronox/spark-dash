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
kernel modules get built, on any of the 3 GX10 nodes. If a piece of the
monitoring stack can't be satisfied that way, it gets descoped rather than
quietly becoming a host change — see the `spark_hwmon` decision below for a
concrete example of that tradeoff actually being made.

## Why this matters here specifically

DGX OS on the GX10 is comparatively hard to reinstall/reimage cleanly compared
to a generic Linux box, and it's shared with the actual inferencing workload —
the whole point of the hardware. Keeping every monitoring component
containerized and disposable means:

- A bad exporter config is `docker compose down`, not a broken host package.
- The 3-node rollout (Phase 2) is "copy a Compose file," not "re-run a
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
| `spark-dash-backend` | ours | FastAPI; also serves the built Svelte assets, so there's no separate web server. See [app-design.md](app-design.md). |
| `cloudflared` | already running | Reuse the existing tunnel — add a hostname/route + Access policy rather than standing up a second connector. |

Two new containers, plus a route on infrastructure that already exists.

### Sizing

2 vCPU / 4GB RAM / ~50GB disk is comfortable for 3 nodes. Prometheus storage is
the only thing that grows: roughly a couple of GB for 30 days at this metric
volume and scrape interval — set real retention in Phase 3 once there's actual
data to measure rather than guessing now.

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

`NODE_ID` is the only value that differs between the three GX10s — everything
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

Settled. Images are built by hand, tagged with the commit, and pinned by tag in
each stack's `.env`.

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

**Build on ONE host per image, not on each node.** All three GX10s are arm64
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
