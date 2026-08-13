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

### Monitoring the monitor

A dead-man's-switch is the one gap this design can't close from the inside: if
the monitoring VM is down, it can't alert that it's down. Phase 3 should add an
external heartbeat (a healthchecks.io-style ping from the backend, or a check
from another Proxmox guest). Flagged rather than solved here.

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

Exact image tags, registry, and whether we self-host a registry or just build
locally are implementation details for Phase 1 — flagged as non-blocking here.
