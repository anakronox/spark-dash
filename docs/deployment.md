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

| Service | Image source | Host access needed |
|---|---|---|
| `node-exporter` | official `prom/node-exporter` | read-only bind mounts: `/proc`, `/sys`, `/` |
| GPU baseline exporter | `dcgm-exporter` (official NVIDIA image) **or** `dgx-spark-prometheus` (see below) | `--gpus all`; `dcgm-exporter` additionally needs `--cap-add SYS_ADMIN` |
| `gb10-node-exporter` | ours, custom | `--gpus all` (NVML/`nvidia-smi` access via Container Toolkit); read-only bind mounts `/proc`, `/sys` for UMA memory + PSI |
| `llama-router-exporter` | ours, custom | none beyond network access to the local llama.cpp router container(s) |

vLLM itself needs no sidecar — Prometheus scrapes its native `/metrics`
directly; it's already a container on the existing inference stack.

**`dgx-spark-prometheus` packaging note:** upstream ships a systemd/host-binary
install path (`go build`, `sudo cp` to `/usr/local/bin`, a `.service` file).
We don't use that path. It's a self-contained static Go binary whose only
inputs are `/proc`, `/sys`, and shelling out to `nvidia-smi` — trivially
wrapped in our own minimal Dockerfile (multi-stage: `golang` build stage,
distroless/`scratch`-ish runtime stage) with the same bind mounts as
`node-exporter` plus `--gpus all`. This is a small, one-time packaging task
(see [roadmap.md](roadmap.md)), not a blocker.

**Networking:** our exporters need to reach the existing llama.cpp/vLLM
containers (for `llama-router-exporter`, and for Prometheus to scrape vLLM's
`/metrics`). Since those already run via their own `docker compose` stacks per
host, our monitoring stack either joins the same Docker network (declared as
`external` in our Compose file) or reaches them via ports already published to
the host. Prefer joining the existing network where possible — avoids relying
on published ports that might change.

## Central services (Prometheus + backend + frontend)

Also plain containers — `prom/prometheus` official image, and our own backend
(FastAPI, proposed) and frontend (React/Vite, proposed) images. Where these
run (one of the GX10s vs. a separate always-on host) is still an
[open decision](roadmap.md#open-decisions) — either way, it's a Compose
project, not a host install.

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
choice — not an oversight. Everything else in `gb10-node-exporter` (UMA
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

  gpu-baseline-exporter:
    image: nvcr.io/nvidia/k8s/dcgm-exporter:<pinned-tag>
    deploy: {resources: {reservations: {devices: [{driver: nvidia, capabilities: [gpu]}]}}}
    cap_add: [SYS_ADMIN]
    networks: [inference-net]

  gb10-node-exporter:
    image: <our-registry>/gb10-node-exporter:<tag>
    deploy: {resources: {reservations: {devices: [{driver: nvidia, capabilities: [gpu]}]}}}
    volumes:
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
    networks: [inference-net]

  llama-router-exporter:
    image: <our-registry>/llama-router-exporter:<tag>
    environment:
      - ROUTER_URL=http://llama-router:8080  # existing container, same network
    networks: [inference-net]

networks:
  inference-net:
    external: true  # the existing llama.cpp/vLLM compose network
```

Exact image tags, registry, and whether we self-host a registry or just build
locally on each node are implementation details for Phase 1 — flagged as
non-blocking here.
