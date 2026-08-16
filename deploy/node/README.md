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
   # On a GX10 (arm64 — build native, no QEMU needed).
   # Clones if absent, updates if already there: `git clone` onto an existing
   # directory fails, and it's easy to miss that error and then build from a
   # stale checkout.
   REPO=/docker/spark-dash-homegrown
   git clone https://forgejo.indielab.tech/brian/spark-dash-homegrown.git "$REPO" \
     || git -C "$REPO" pull
   cd "$REPO"

   docker login forgejo.indielab.tech      # Forgejo token with package write
   ./scripts/publish-images.sh agent
   ```

2. **Each node needs `docker login forgejo.indielab.tech`** if the package is
   private, so it can pull.

## Configure

Nothing needs to differ per node. The agent reads the **host's** hostname from
a bind-mounted `/etc/hostname` and uses it as its node id, so this same stack
deploys unchanged to all three GX10s — one stack repo, no per-node override to
forget.

> It has to be `/etc/hostname`, not `/proc/sys/kernel/hostname`. The procfs
> entry is UTS-namespace-aware and returns the *container's* hostname even
> through a bind-mounted host `/proc` — which is Docker's container id, and
> changes on every recreate.

Check `hostname` on the box first: that string becomes the `node` label on
every metric, and it must match what `SPARK_NODES` uses on the central stack,
or the agent's metrics won't join node-exporter's.

```bash
# Required: .env is not tracked, so a fresh clone has no config.
cp .env.example .env
$EDITOR .env
```

| Variable | Notes |
|---|---|
| `NODE_ID` | **Optional.** Defaults to the host's hostname. Set it only if that hostname is a poor label. Becomes the `node` label on every metric — keep it stable, since renaming orphans that node's history. |
| `LLAMA_ROUTER_URLS` | Comma-separated router base URLs. |
| `LLAMA_METRICS_ROUTERS` | **Leave empty unless certain.** Opt-in allowlist for `/metrics?model=` requests, which LOAD the model on an autoload router. |
| `VLLM_URLS` | Comma-separated vLLM `/metrics` endpoints. |

## Where things land

Dockhand deploys the stack from its git repo into
`/docker/spark-dash-stack-node/` — a clone of `spark-dash-stack-node`, with the
untracked `.env` alongside it.

**There is no second directory to create.** The central stack splits config in
git from a Prometheus TSDB on disk; this one has no persistent data at all. The
agent holds nothing across restarts, and its only mounts are read-only views of
the host's `/proc` and `/sys`. Everything it reports is derived live from the
machine, and history lives in Prometheus on the monitoring VM. So there's no
`prepare-host.sh` here and nothing to back up.

The main repo — needed for `publish-images.sh` and the validation scripts —
follows the usual convention:

```bash
REPO=/docker/spark-dash-homegrown
git clone https://forgejo.indielab.tech/brian/spark-dash-homegrown.git "$REPO" || git -C "$REPO" pull
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

## Updating

**Check for new settings after every pull.** `.env` is untracked, so a pull
can't clobber your configuration — but it can't update it either. A variable
added upstream won't appear in your `.env`, and a stale value there silently
overrides any new default in `compose.yaml`:

```bash
diff <(grep -oE '^[A-Z_]+' .env.example | sort) \
     <(grep -oE '^[A-Z_]+' .env | sort)
```

Anything listed only on the left is new — copy it across from `.env.example`.

### Rolling out a new image

**Today (Dockhand not yet orchestrated), rollout is manual and pinning is the
mechanism:**

```bash
# In .env — the tag publish-images.sh printed
AGENT_IMAGE=forgejo.indielab.tech/brian/spark-dash-agent:255e10e
```

```bash
docker compose up -d spark-dash-agent
```

Pin rather than `docker compose up -d --pull always`: `--pull always` re-pulls
every image including the pinned third-party ones, so a transient registry
outage fails a deploy that only needed to change our own container.

#### Where this is going: `:latest` + a daily pull

Dockhand is configured per managed environment to **pull new images once a day
in off-hours**, so once it is actually driving this stack the `.env` will track
`:latest` and converge on its own. That is the settled design — see
[roadmap.md](../../docs/roadmap.md) open decision 6 — and it is why images are
pushed with both a sha tag *and* `:latest`.

Two consequences of that switch, worth knowing before it happens:

- **The build becomes the deploy action.** Pushing to `main` ships nothing;
  images exist only when `publish-images.sh` runs. Running it will mean "this
  goes live on every node in the environment within 24 hours", where today a
  pin edit still sits between building and running.
- **Configuration stops recording what is deployed.** `:latest` is not an
  answer to "what was running on the 12th". That is what
  `sparkdash_agent_build_info` is for — it records what *actually ran*, which
  is a better answer than a pin, since a pin only ever recorded intent.

Pinning does not disappear; it becomes the **exception path**. When a bad build
lands overnight, pin the last-good sha and redeploy — no rebuild needed — then
return to `:latest` once it is fixed.

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
- **This stack joins no external network.** The agent reaches the routers and
  vLLM by host address, which works from a plain bridge network. That keeps the
  stack free of any dependency that has to exist before it can start — an
  external network that isn't there fails the whole stack with
  `network ... declared as external, but could not be found`.
