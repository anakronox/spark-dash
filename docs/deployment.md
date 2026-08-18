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

### GPU baseline exporter — deferred to Phase 4

`dcgm-exporter` / `dgx-spark-prometheus` are **not** in the Phase 1 stack. Our
agent already reads NVML directly for utilization/temp/power/clocks, and
`dcgm-exporter`'s headline advantage (GPU memory) is exactly the number that
unified memory breaks on GB10. What DCGM would still add is deep profiling
telemetry — SM/tensor-core activity, memory bandwidth — which is genuinely
useful for inference tuning but is not MVP-critical and has uncertain GB10
support. Revisit in Phase 4.

If `dgx-spark-prometheus` is ever adopted, note that upstream ships a
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

Most other Spark dashboards run directly on the Spark. This one deliberately
does not. A single-host mode for users without spare hardware is roadmap
workstream J, and it is opt-in with its cost stated rather than the default.

## Central stack — a dedicated Proxmox VM (settled)

**Decision: the central stack runs on its own Docker host — a dedicated VM on
the existing Proxmox cluster — not on any GX10.**

### Why not on a GX10

The argument is failure domains, not resources:

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

Settled. Images are built by hand with `scripts/publish-images.sh`, tagged with
the commit, and pinned by tag in each stack's `.env`. Four steps, in order:

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

| | |
|---|---|
| `--no-push` | build locally and stop; no registry or `docker login` needed |
| `--tag TAG` | override the tag (default: short git sha, `-dirty` if the tree is) |
| `--no-latest` | push only the tag, not `:latest` |
| `REGISTRY=` / `OWNER=` | override the registry; both default to the clone's own `origin` remote |

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
