# GX10 node stack

Two containers — stock `node-exporter` plus our `spark-dash-agent`. Deployed by
Dockhand from git; nothing is installed on the base OS. See
[../../docs/deployment.md](../../docs/deployment.md).

This directory is a **self-contained stack**: `compose.yaml` at its root, so it
works either as a subpath of this repo or copied verbatim into a standalone
stack repo.

## Prerequisites

1. **The image must be published** for arm64. Dockhand deploys stacks, it
   doesn't build them:

   ```bash
   # On a GX10 (arm64 — build native, no QEMU needed)
   git clone https://forgejo.indielab.tech/brian/spark-dash-homegrown.git
   cd spark-dash-homegrown
   docker login forgejo.indielab.tech      # Forgejo token with package write
   ./scripts/publish-images.sh agent
   ```

2. **Each node needs `docker login forgejo.indielab.tech`** if the package is
   private, so it can pull.

## Configure

Nothing needs to differ per node. The agent reads the **host's** hostname from
the mounted procfs (`/proc/sys/kernel/hostname`) and uses it as its node id, so
this same stack deploys unchanged to all three GX10s — one stack repo, no
per-node override to forget.

```bash
cp .env.example .env
$EDITOR .env
```

| Variable | Notes |
|---|---|
| `NODE_ID` | **Optional.** Defaults to the host's hostname. Set it only if that hostname is a poor label. Becomes the `node` label on every metric — keep it stable, since renaming orphans that node's history. |
| `LLAMA_ROUTER_URLS` | Comma-separated router base URLs. |
| `LLAMA_METRICS_ROUTERS` | **Leave empty unless certain.** Opt-in allowlist for `/metrics?model=` requests, which LOAD the model on an autoload router. |
| `VLLM_URLS` | Comma-separated vLLM `/metrics` endpoints. |
| `INFERENCE_NETWORK` | Existing llama.cpp/vLLM Compose network (`docker network ls`). |

## Where things land

hawser syncs the stack from git into `/docker/hawser/spark-dash-stack-node/`.

**There is no `/docker/spark-dash-stack-node/` to create.** This stack has no
persistent data: the agent holds nothing across restarts, and its only mounts
are read-only views of the host's `/proc` and `/sys`. Everything it reports is
derived live from the machine, and history lives in Prometheus on the
monitoring VM. So there's no `prepare-host.sh` here and nothing to back up.

The main repo — needed for `publish-images.sh` and the validation scripts —
follows the usual convention:

```bash
git clone https://forgejo.indielab.tech/brian/spark-dash-homegrown.git /docker/spark-dash-homegrown
```

## Verify

```bash
curl -s localhost:9500/health | jq
curl -s localhost:9500/snapshot | jq
```

Then, from a clone of the main repo:

```bash
./scripts/validate-on-gx10.sh
```

That checks the GB10-specific things that can only be confirmed on real
hardware: unified-memory detection, agreement with `/proc/meminfo`, the
milliwatt-to-watt power conversion, clock load-gating, and host PID namespace
visibility for process attribution.

## The safety property

The agent reads router state via `/v1/models` and `/props`, neither of which
takes a `model` parameter and neither of which can cause a load. It issues
`/metrics?model=` **only** when the router is in `LLAMA_METRICS_ROUTERS` *and*
the model already reports `loaded`.

With `LLAMA_METRICS_ROUTERS` empty — the default — no such request is ever
issued to any router. You still get model names, states and capacity; only
per-model throughput and KV-cache detail is withheld.

To prove it on your hardware:

```bash
./scripts/soak-test-autoload.sh 10
```

## Notes

- **`pid: "host"`** is required for per-process GPU attribution: NVML returns
  host PIDs, meaningless inside a private PID namespace.
- The agent runs as **non-root** and only ever reads. Process names resolve
  fine; `/proc/<pid>/cwd` may not, which is why runtime detection also matches
  on command-line flags.
- **If your runtimes aren't on a shared Compose network**, remove the
  `inference` network from `compose.yaml` and point `LLAMA_ROUTER_URLS` /
  `VLLM_URLS` at host addresses instead (e.g. `http://192.168.50.61:8001`).
