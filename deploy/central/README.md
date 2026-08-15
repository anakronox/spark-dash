# Central deployment (monitoring VM)

Two containers — Prometheus and the backend — on a dedicated Proxmox VM.
Deliberately **not** on a GX10: see
[../../docs/deployment.md](../../docs/deployment.md#central-stack--a-dedicated-proxmox-vm-settled).

This directory is a **self-contained stack**: `compose.yaml` at its root, so it
works either as a subpath of this repo or copied verbatim into a standalone
stack repo.

## Prerequisites

The image must be published first — Dockhand deploys stacks, it doesn't build
them. Build on the monitoring VM so the architecture matches:

```bash
# Clones if absent, updates if already there: `git clone` onto an existing
# directory fails, and it's easy to miss that error and then build from a
# stale checkout.
REPO=/docker/spark-dash-homegrown
git clone https://forgejo.indielab.tech/brian/spark-dash-homegrown.git "$REPO" || git -C "$REPO" pull
cd "$REPO"

docker login forgejo.indielab.tech      # Forgejo token with package write
./scripts/publish-images.sh backend
```

## Configure

```bash
# Required: .env is not tracked, so a fresh clone has no config.
cp .env.example .env
$EDITOR .env          # set SPARK_NODES

# Create the data directories with the ownership the containers need.
# Docker auto-creates bind-mount sources as ROOT, which neither container can
# write to — so this step is required, not optional.
sudo mkdir -p /docker/spark-dash-stack-central/{prometheus,targets}
sudo chown 65534:65534 /docker/spark-dash-stack-central/prometheus
sudo chown 10002:10002 /docker/spark-dash-stack-central/targets

docker compose up -d
```

Both containers run as non-root, and each needs write access to one directory:

| Directory | UID | What | Symptom if wrong |
|---|---|---|---|
| `$DATA_ROOT/prometheus` | `65534` | Prometheus TSDB (the one that grows) | Prometheus crash-loops: `panic: Unable to create mmap-ed active query log` |
| `$DATA_ROOT/targets` | `10002` | scrape targets rendered by the backend | backend logs `could not write ... is the volume writable?`; Prometheus scrapes nothing |

That first panic is worth recognising — it reads like a Prometheus bug, but it
is always this.

If you'd rather run both as some other uid, set `PUID`/`PGID` in `.env`
instead of chowning. Leaving them unset keeps each container on its own
non-root user, which is the better default.

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

### Changing prometheus.yml, alerts.yml or alertmanager.yml

**A `git pull` is not enough, and neither is a reload.** These three are
bind-mounted as *single files*, and a file bind-mount follows the **inode**, not
the path. `git pull` replaces a file rather than editing it in place, so the new
content lands on a new inode and the container keeps reading the old one. A
SIGHUP or `POST /-/reload` then faithfully re-reads the stale file and reports
success.

That failure is silent: the host shows your new alert rule, Prometheus says the
config reloaded fine, and the rule simply isn't there. Observed 2026-08-15 —
host inode 427369, container still on 420933.

Recreate the container so the mount re-resolves:

```bash
docker compose up -d --force-recreate prometheus     # or alertmanager
```

`--force-recreate` is required: a plain `up -d` sees an unchanged compose config
and does nothing.

```bash
# Confirm it actually took, rather than trusting the reload:
docker exec sparkdash-prometheus grep -c AgentBuildSkew /etc/prometheus/alerts.yml
```

> This applies to anything that delivers config by replacing files — including
> a future Dockhand pull. If Dockhand pulls the orchestration repo and runs a
> plain `docker compose up -d`, a config-only change will **not** take effect.

### Rolling out a new image

Pin the tag the publish script printed, rather than chasing `:latest`:

```bash
# In .env
BACKEND_IMAGE=forgejo.indielab.tech/brian/spark-dash-backend:9c2b41f
```

```bash
docker compose up -d
```

Two reasons to pin rather than `docker compose up -d --pull always`:

- **Traceability.** `:latest` moves; a sha tag says exactly which commit is
  running, and Dockhand redeploys on the git change to `.env` rather than on an
  image that silently drifted underneath it.
- **Fewer moving parts.** `--pull always` re-pulls every image including the
  pinned third-party ones, so a transient Docker Hub outage fails a deploy that
  only needed to change our own container.

## Access model

| Port | Service | Reachable from |
|---|---|---|
| `8080` | dashboard | LAN **and** the Cloudflare tunnel, behind Google OAuth |
| `9090` | Prometheus | LAN only |
| `9093` | Alertmanager | LAN only |

Only the dashboard is published externally. Prometheus and Alertmanager have no
authentication of their own, and they don't need it while the LAN is the trust
boundary — nothing routes them off the network.

That's also why the dashboard is read-only: it's the one service with an
external path, so it deliberately can't load, unload or kill anything even if
someone reached it.

To lock the internal services down further, set `PROM_BIND=127.0.0.1` and
`ALERTMANAGER_BIND=127.0.0.1` and reach them over SSH:

```bash
ssh -L 9090:localhost:9090 -L 9093:localhost:9093 brian@192.168.50.156
```

## Alerting

Prometheus evaluates `alerts.yml` and hands firing alerts to Alertmanager,
which groups and routes them. The dashboard shows what's firing; Alertmanager
decides what reaches you.

### Notifications (ntfy)

**You don't need to run anything.** [ntfy.sh](https://ntfy.sh) is a free public
push service with no account, no signup and no API key. You invent a topic
name, subscribe to it in an app, and anything POSTed to that topic arrives as a
push notification.

Self-hosting is possible and covered at the end, but read the tradeoff first —
it's not obviously the better choice here.

#### How the security model works

**The topic name is the only access control.** There are no credentials on
ntfy.sh: anyone who knows or guesses your topic can read every alert you
receive and send you notifications of their own.

Your alerts name your nodes, IP addresses and models, so treat the topic like a
password:

- `spark-dash` — guessable in seconds. Don't.
- `spark-dash-alerts` — still guessable.
- `spark-dash-k7mq2vx9wp4n` — fine.

Generate one rather than inventing it:

```bash
echo "spark-dash-$(head -c 9 /dev/urandom | base32 | tr '[:upper:]' '[:lower:]' | tr -d '=')"
```

This is also why the URL is kept out of git — the repo is public, and the
topic URL is the whole secret.

#### Setup

**1. Pick a topic and write it to the secrets file.**

```bash
TOPIC="spark-dash-$(head -c 9 /dev/urandom | base32 | tr '[:upper:]' '[:lower:]' | tr -d '=')"
echo "Your topic: $TOPIC"          # note this down — you need it in the app

sudo mkdir -p /docker/spark-dash-stack-central/secrets
echo -n "https://ntfy.sh/$TOPIC?template=alertmanager" \
  | sudo tee /docker/spark-dash-stack-central/secrets/ntfy-url > /dev/null

sudo chown -R 65534:65534 /docker/spark-dash-stack-central/secrets
sudo chmod 600 /docker/spark-dash-stack-central/secrets/ntfy-url
```

Two details that will bite otherwise:

- **`echo -n`** — a trailing newline becomes part of the URL and every POST
  fails with a confusing error.
- **`?template=alertmanager`** — ntfy's built-in template that renders the
  webhook into a readable alert. Without it you get a wall of raw JSON on your
  lock screen.

**2. Subscribe on whatever you'll actually look at.**

| | |
|---|---|
| iOS | [ntfy on the App Store](https://apps.apple.com/us/app/ntfy/id1625396347) → **+** → enter the topic name |
| Android | [Play Store](https://play.google.com/store/apps/details?id=io.heckel.ntfy) or [F-Droid](https://f-droid.org/en/packages/io.heckel.ntfy/) → **+** → enter the topic name |
| Desktop / browser | Open `https://ntfy.sh/<your-topic>` and allow notifications |
| Terminal | `ntfy subscribe <your-topic>` |

Enter only the **topic name** in the app, not the full URL.

**3. Start Alertmanager and test.**

```bash
docker compose up -d

# Should arrive on your phone within a couple of seconds.
curl -d "spark-dash test" "$(sudo cat /docker/spark-dash-stack-central/secrets/ntfy-url)"
```

If nothing arrives, `docker logs sparkdash-alertmanager` will name the reason —
usually a stray newline in the URL file, or the topic in the app not matching.

#### What arrives

A firing alert looks roughly like:

```
[FIRING:1] GpuThrottled sparky
GPU on sparky throttled under sustained load
Clock has stayed below the throttle threshold while the GPU is busy.
On GB10 this usually means power delivery rather than heat — check the
PSU and the power cable before assuming thermal.
```

Recoveries arrive too (`send_resolved: true`). Without them a resolved alert
just goes quiet, which is indistinguishable from you having missed it.

Critical alerts are delivered faster (10s rather than 45s batching) and repeat
every 4h instead of 12h.

#### Self-hosting instead

One container, and only the hostname in the secrets file changes:

```yaml
services:
  ntfy:
    image: binwiederhier/ntfy:latest
    command: serve
    ports: ["8090:80"]
    volumes:
      - /docker/ntfy/cache:/var/cache/ntfy
```

**The tradeoff is real, though.** A LAN-only ntfy means no notifications when
you're away from home — which is exactly when an unattended node going down
matters most. To fix that you'd publish it through the Cloudflare tunnel, which
means a second externally-reachable service to secure, on top of the dashboard.

ntfy.sh with a random topic avoids that entirely. The tradeoff you accept
instead is that a third party sees your alert text — node names, IPs, model
names. Neither is wrong; pick the one whose downside you mind less.

### What fires

| Alert | Fires when | Severity |
|---|---|---|
| `NodeAgentDown` | no metrics from a node for 2m | critical |
| `GpuThrottled` | clock below threshold under sustained load, 5m | critical |
| `GpuTemperatureCritical` | GPU above 94°C for 2m | critical |
| `MemoryPressureCritical` | PSI CRITICAL for 2m | critical |
| `GpuTemperatureHigh` | GPU above 88°C for 10m | warning |
| `MemoryHighWithSwap` | above 85% memory *and* swap in use, 10m | warning |
| `RouterUnreachable` | a llama.cpp router unreachable for 5m | warning |
| `GpuClockLocked` | an external clock cap in place for 30m | warning |
| `PrometheusStorageFillingUp` | disk predicted full within a week | warning |

Every rule has a `for:` duration — a GPU touching 90°C for ten seconds is
weather, ten minutes is a fault. Alerting on instantaneous values is how you
train yourself to ignore alerts.

Two inhibit rules keep a single failure from producing a pile: a node being
down suppresses its other alerts, and critical suppresses warning for the same
condition on the same node.

### Thresholds worth revisiting

The temperature and PSI numbers are marked `[CALIBRATE]` in `alerts.yml`. The
GX10 runs at ~84°C during routine ComfyUI work without throttling, so the
generic 80°C line would fire constantly — 88/94 was chosen from that
observation. The PSI bands are still guesses. See issue #30.

## Clusters and standalone nodes

Not every node is part of a cluster. Prefix grouped nodes with `group/`:

```bash
SPARK_NODES=sparky=192.168.50.61,pair/spark2=192.168.50.62,pair/spark3=192.168.50.63
```

Here `sparky` stands alone and `spark2`/`spark3` are clustered.

This affects capacity arithmetic, not just presentation. Clustered nodes pool
memory for distributed inference, so a model can span the group and their
combined free space is genuine capacity. Ungrouped nodes can't, so free memory
is summed **within** a group and never across groups — a cluster-wide total
would report capacity that doesn't exist.

The dashboard draws grouped nodes in a labelled frame with their pooled free
space; standalone nodes get no frame, because there's nothing to combine. The
`group` label is also written to Prometheus targets, so `sum by (group)` in
PromQL aggregates history the same way.

## Adding a node

One line, one place:

```bash
# deploy/central/.env
SPARK_NODES=gx10-1=192.168.50.61,gx10-2=192.168.50.62
```

```bash
docker compose up -d backend
```

That's the whole change. The backend renders Prometheus's scrape targets from
`SPARK_NODES`, so there's no second inventory to keep in sync, and Prometheus
picks the new targets up on its next `file_sd` refresh without restarting.

`agents.yml` and `node-exporters.yml` under `targets/` are **generated** — they
are gitignored and should not be edited. `vllm.yml` is hand-maintained, because
vLLM instances don't map one-per-node.

## Where things land

Everything lives under `/docker/spark-dash-stack-central/` — a Dockhand clone of
the stack repo, with `DATA_ROOT` in `.env` pointing at that same directory. Two
lifecycles share one path:

| What | Managed by | Tracked in git? |
|---|---|---|
| `compose.yaml`, `prometheus.yml`, `alerts.yml`, `alertmanager.yml` | the stack repo | yes |
| `targets/vllm.yml` | hand-maintained | yes |
| `.env` | edited on the host | no — gitignored |
| `prometheus/` (TSDB), `alertmanager/`, `secrets/` | the containers, at runtime | no — untracked |
| `targets/agents.yml`, `targets/node-exporters.yml` | the backend, from `SPARK_NODES` | no — untracked |

Config comes from git; data does not. Keeping data in a findable place rather
than a named volume under `/var/lib/docker` is the point of `DATA_ROOT`, and it
can be pointed elsewhere if you'd rather separate the two physically.

> **Only `.env` is gitignored.** The runtime directories are merely *untracked*,
> so `git add -A` in this directory would happily stage the Prometheus TSDB.
> Use targeted `git add`, or add them to `.gitignore`.

> **`DATA_ROOT` defaults to this same directory,** which makes `./targets` and
> `${DATA_ROOT}/targets` the same host path — so the container sees one
> directory mounted at both `/etc/prometheus/targets/static` and
> `.../generated`. Harmless, because each scrape job names the file it wants,
> but it does mean the separation described in `prometheus.yml` is notional
> unless `DATA_ROOT` is pointed somewhere else.

> Earlier revisions of this file claimed the config lived under
> `/docker/hawser/spark-dash-stack-central/`. It does not — that path doesn't
> exist on the monitoring VM. hawser's own convention places stacks under
> `/docker/hawser/<stack>/`, but the spark-dash stacks are deployed by Dockhand
> and sit top-level, the same as the node stack. Verified on `sparkmon`
> 2026-08-15.

The main repo — needed for `publish-images.sh` and the validation scripts —
follows the usual convention:

```bash
REPO=/docker/spark-dash-homegrown
git clone https://forgejo.indielab.tech/brian/spark-dash-homegrown.git "$REPO" || git -C "$REPO" pull
```

### Two target directories

| Path in container | Source | Contents |
|---|---|---|
| `targets/generated/` | Docker volume, written by the backend | `agents.yml`, `node-exporters.yml` — from `SPARK_NODES` |
| `targets/static/` | bind mount from the stack dir | `vllm.yml` — hand-maintained |

They're separate on purpose. A single directory can't work: the volume holding
the generated files would mount *over* the stack directory's own files, so
`vllm.yml` would silently never be scraped.

## Verify

These run on the VM. It's headless, so use `curl` over SSH rather than a
browser — or open the same URLs from a LAN machine using the VM's address.

```bash
# Backend liveness and self-assessment (this is what UptimeKuma watches).
curl -s localhost:8080/health | jq

# Alert rules: all should be "inactive" (loaded, not firing) on a healthy day.
curl -s localhost:9090/api/v1/rules | jq -r '.data.groups[].rules[] | "\(.state)\t\(.name)"'

# Anything currently firing.
curl -s localhost:8080/api/alerts | jq

# What the backend thinks the cluster is.
curl -s localhost:8080/api/nodes | jq

# Confirm the rendered scrape targets look right.
docker compose exec backend cat /etc/prometheus/targets/generated/agents.yml

# Confirm Prometheus actually picked them up — every target should be "up".
curl -s localhost:9090/api/v1/targets | jq '.data.activeTargets[] | {job: .labels.job, node: .labels.node, health}'
```

### What "healthy" looks like

- `/health` reports `status: ok`. `degraded` means Prometheus is unreachable,
  the inventory is empty, or no node answers — all of which should be visible
  rather than passing a naive uptime check.
- Every node in `/api/nodes` shows `up: true`. A node that's down shows
  `up: false` with `health: critical`, not a missing row — a missing tile is
  easy to overlook.
- Prometheus targets are all `up`. A target stuck `down` usually means the
  agent isn't running on that node, or a firewall is blocking 9500/9100.

## Notes

- **Prometheus is bound to localhost** by default (`PROM_BIND`). It has no auth
  of its own, and the dashboard is the intended way in. Only expose it on the
  LAN if you want its query UI directly.
- **cloudflared is not defined here.** It already runs on this host; adding the
  dashboard is a tunnel route pointed at `:8080`, not a second connector.
- **Retention** (`PROM_RETENTION`) is a placeholder 30d. Phase 3 sets a real
  value from observed disk usage rather than a guess.
- The backend needs write access to the shared `prom-targets` volume. Ownership
  is seeded from the image, so this works on a freshly created volume without
  running as root.
