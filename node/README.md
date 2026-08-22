# GX10 node stack

Two containers — stock `node-exporter` plus our `spark-dash-agent`. Cloned from
git and started by hand; nothing is installed on the base OS. See
[../docs/deployment.md](../docs/deployment.md).

This directory is a self-contained stack: `compose.yaml` is the only file it
needs. Its bind mounts are all absolute host paths (`/proc`, `/sys`,
`/etc/hostname`, `/`), so there's no data directory to create and no
`DATA_ROOT` to set. The `.env` beside it is host-local and gitignored.

## Prerequisites

1. **Build the agent image on this node** — nothing here builds it on deploy,
   and a GX10 is arm64, so building where it runs avoids cross-building
   entirely. No registry and no `docker login` are involved:

   ```bash
   # On a GX10 (arm64 — build native, no QEMU needed).
   # Clones if absent, updates if already there: `git clone` onto an existing
   # directory fails, and it's easy to miss that error and then build from a
   # stale checkout.
   REPO=/docker/spark-dash-homegrown   # your choice of path
   SRC=<the URL you cloned this from>
   git clone "$SRC" "$REPO" || git -C "$REPO" pull
   cd "$REPO"

   ./scripts/build-images.sh agent         # spark-dash-agent:latest, locally
   ```

2. **If you pull from a registry instead** — the maintainer path — each node
   also needs `docker login <registry>`, plus `AGENT_IMAGE` and
   `PULL_POLICY=always` in its `.env`. See
   [../docs/deployment.md](../docs/deployment.md#building-and-shipping-images).

## Configure

**Which routers and engines this node serves is set on the monitoring host**,
in `cluster.yml` — the agent asks the backend what to poll. Nothing else here
differs between nodes either: the agent reads the host's hostname from a
bind-mounted `/etc/hostname` and uses it as its node id. So the same stack
deploys unchanged to every GX10.

> It has to be `/etc/hostname`, not `/proc/sys/kernel/hostname`. The procfs
> entry is UTS-namespace-aware and returns the *container's* hostname even
> through a bind-mounted host `/proc` — which is Docker's container id, and
> changes on every recreate.

Check `hostname` on the box first: that string becomes the `node` label on
every metric, and it must match the `id` used in `cluster.yml` on the central stack,
or the agent's metrics won't join node-exporter's.

```bash
# Required: .env is not tracked, so a fresh clone has no config.
cp .env.example .env
$EDITOR .env
```

| Variable | Notes |
|---|---|
| `BACKEND_URL` | **The only required edit.** Where the agent fetches its runtime config from, and the same value on every node. |
| `NODE_ID` | **Optional.** Defaults to the host's hostname. Set it only if that hostname is a poor label. Becomes the `node` label on every metric — keep it stable, since renaming orphans that node's history. |

The three variables below are a **fallback for a node not yet managed
centrally** — leave them empty when `cluster.yml` lists this node, which is the
normal case. They exist so a node can be brought up before the central stack
knows about it, and are ignored once it does.

| Fallback variable | Notes |
|---|---|
| `LLAMA_ROUTER_URLS` | Comma-separated router base URLs. |
| `LLAMA_METRICS_ROUTERS` | **Leave empty unless certain.** Opt-in allowlist for `/metrics?model=` requests, which LOAD the model on an autoload router. |
| `VLLM_URLS` / `SGLANG_URLS` | Comma-separated `/metrics` endpoints, per engine. |

## Where things land

`/docker/spark-dash-homegrown/node/` — this directory, in a clone of the one
repo, with the gitignored `.env` alongside `compose.yaml`.

**There is no second directory to create.** The central stack splits config in
git from a Prometheus TSDB on disk; this one has no persistent data at all. The
agent holds nothing across restarts, and its only mounts are read-only views of
the host's `/proc` and `/sys`. Everything it reports is derived live from the
machine, and history lives in Prometheus on the monitoring VM. So there's no
`prepare-host.sh` here and nothing to back up.

The main repo — needed for `publish-images.sh` and the validation scripts —
follows the usual convention:

```bash
REPO=/docker/spark-dash-homegrown   # your choice of path
SRC=<the URL you cloned this from>
git clone "$SRC" "$REPO" || git -C "$REPO" pull
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

**Rollout is manual, and per node.** Building locally — the default — it is
three commands on each GX10:

```bash
git -C "$REPO" pull
"$REPO"/scripts/build-images.sh agent
docker compose up -d spark-dash-agent
```

**If you deploy from a registry instead, pin the tag** that
`publish-images.sh` printed:

```bash
# In .env
AGENT_IMAGE=<registry>/<owner>/spark-dash-agent:255e10e
```

```bash
docker compose up -d spark-dash-agent
```

Pin rather than `docker compose up -d --pull always`: `--pull always` re-pulls
every image including the pinned third-party ones, so a transient registry
outage fails a deploy that only needed to change our own container.

#### Why not `:latest`

Nothing pulls images on a schedule here — every deploy is someone running
`up -d` on a node. `:latest` would mean the running build is whatever happened
to be in the registry the last time that command ran, with no record of which,
and an `up -d` on an unchanged file would silently change the agent version.

Rolling back is editing that one line.

**This is why `AgentBuildSkew` matters more than it looks.** Nothing brings a
missed node into line overnight, so a node forgotten during a rollout stays on
the old build indefinitely. A stale agent has twice presented as a *missing
feature* rather than as a stale agent, costing a debugging round trip each
time — that alert is the only thing that notices.

`sparkdash_agent_build_info` records what **actually ran**, which is a better
answer to "what was on node 3 on the 12th" than any pin, since a pin only ever
recorded intent.

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
