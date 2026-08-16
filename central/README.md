# Central deployment (monitoring VM)

Two containers — Prometheus and the backend — on a dedicated Proxmox VM.
Deliberately **not** on a GX10: see
[../docs/deployment.md](../docs/deployment.md#central-stack--a-dedicated-proxmox-vm-settled).

This directory is a **self-contained stack**: `compose.yaml` at its root, and
every bind mount in it is `./something` relative to this directory. Config is
tracked in git; the containers' state (`prometheus/`, `alertmanager/`,
`secrets/`, `cluster/`, `targets/`) is written right here and gitignored.

There is no `DATA_ROOT` and no separate stack repo — clone this repo on the
monitoring VM, `cd central`, and start it.

**The relative paths are deliberate.** They are what make a clone runnable
unedited from wherever it lands. Absolute paths would bake one operator's
directory layout into a tracked file, so anyone who cloned somewhere else
would hit broken mounts before getting anything running. If you orchestrate
this with a tool that runs compose from its own directory and you want
absolute paths, put them in *your* copy, outside this repo — that way the
drift costs you one diff instead of shipping a broken default to everyone.

## Prerequisites

The image must be published first — nothing here builds it on deploy. Build on
the monitoring VM so the architecture matches:

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

Everything below is relative to this directory — `cd` here first:

```bash
cd /docker/spark-dash-homegrown/central

# Required: .env is not tracked, so a fresh clone has no config.
cp .env.example .env
$EDITOR .env          # image tag, bind addresses, retention

# The cluster itself — nodes, groups, and what each one serves — lives here,
# not in .env. This is the one file to edit when adding a node.
mkdir -p cluster
cp cluster.yml.example cluster/cluster.yml
$EDITOR cluster/cluster.yml

# Create the data directories with the ownership the containers need.
# Docker auto-creates bind-mount sources as ROOT, which neither container can
# write to — so this step is required, not optional.
mkdir -p prometheus targets alertmanager secrets
sudo chown 65534:65534 prometheus alertmanager
sudo chown 10002:10002 targets

docker compose up -d
```

Both containers run as non-root, and each needs write access to one directory:

| Directory | UID | What | Symptom if wrong |
|---|---|---|---|
| `central/prometheus` | `65534` | Prometheus TSDB (the one that grows) | Prometheus crash-loops: `panic: Unable to create mmap-ed active query log` |
| `central/targets` | `10002` | scrape targets rendered by the backend | backend logs `could not write ... is the volume writable?`; Prometheus scrapes nothing |

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

They live in `config/`, which is mounted as a **directory**. A `git pull` then
takes effect on the next reload — no container recreate, no special flags:

```bash
docker kill -s HUP sparkdash-prometheus     # or POST /-/reload
```

```bash
# Confirm it landed, rather than trusting the reload:
docker exec sparkdash-prometheus grep -c AgentBuildSkew /etc/prometheus/config/alerts.yml
```

> **Why a directory and not three file mounts.** A single-file bind mount
> follows the **inode**, and `git pull` replaces files rather than editing them
> in place. The container went on reading the old inode while the host showed
> the new content — measured here as host inode 2391 against container 430115,
> with a plain `docker compose up -d` reporting "Running" and changing nothing.
> The failure was silent in the worst way: Prometheus reported a clean reload
> and simply did not have the rule.
>
> A directory mount resolves each entry on access, so a replaced file is picked
> up without recreating anything. That is why the vLLM targets worked first time
> while `alerts.yml` did not — one was already inside a directory mount.

### Rolling out a new image

**Rollout is manual, and pinning is the mechanism:**

```bash
# In .env — the tag publish-images.sh printed
BACKEND_IMAGE=forgejo.indielab.tech/brian/spark-dash-backend:9c2b41f
```

```bash
docker compose up -d backend
```

Pin rather than `docker compose up -d --pull always`: `--pull always` re-pulls
every image including the pinned third-party ones, so a transient registry
outage fails a deploy that only needed to change our own container.

#### Why not `:latest`

Nothing pulls images on a schedule here — every deploy is someone running
`up -d`. So `:latest` would mean the running build is whatever happened to be
in the registry the last time that command ran, with no record of which, and
`docker compose up -d` on an unchanged file would silently change the running
version.

An earlier plan had Dockhand pulling daily in off-hours, which would have made
`:latest` converge on its own and turned pinning into an exception path. That
is not how this is deployed, so the pin is the mechanism rather than a
stopgap: `publish-images.sh` prints the sha to paste in, and rolling back is
editing one line.

> **Config files are a separate matter**, but a simpler one than it used to be:
> they live in `config/` as a directory mount, so a pull plus a reload is
> enough — see the section above. A daily image pull will not trigger that
> reload for you, so a config-only change still needs the SIGHUP.

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

mkdir -p secrets
echo -n "https://ntfy.sh/$TOPIC?template=alertmanager" \
  | sudo tee secrets/ntfy-url > /dev/null

sudo chown -R 65534:65534 secrets
sudo chmod 600 secrets/ntfy-url
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
curl -d "spark-dash test" "$(sudo cat secrets/ntfy-url)"
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

Not every node is part of a cluster. Give grouped nodes a `group:`:

```yaml
nodes:
  - id: sparky
    host: 192.168.50.61      # no group — stands alone
  - id: spark2
    host: 192.168.50.62
    group: pair
  - id: spark3
    host: 192.168.50.63
    group: pair
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

One entry, one file — and no restart:

```yaml
# central/cluster/cluster.yml
nodes:
  - id: gx10-1
    host: 192.168.50.61
  - id: gx10-2
    host: 192.168.50.62
    runtimes:
      llama_routers: [{port: 8001, scrape_metrics: true}]
```

That's the whole change. The backend re-reads the file on a TTL, so the node
appears in the live view on its own; it renders Prometheus's scrape targets
from that same entry, so there's no second inventory to keep in sync, and
Prometheus picks the new targets up on its next `file_sd` refresh without
restarting either.

The new node's own stack needs nothing node-specific — it asks the backend
what it should be polling, and the answer is the `runtimes` block above. That
is what lets one stack config deploy unchanged to every GX10.

If the file doesn't parse, the backend keeps the node list it already had and
says so loudly in its log rather than dropping back to an older source — check
`docker logs sparkdash-backend` if an edit appears to do nothing.

Everything under `targets/` is **generated** — gitignored, rewritten by the
backend, never hand-edited. The one hand-maintained scrape target list lives in
`config/vllm-targets.yml`, because vLLM instances don't map one-per-node and so
can't be derived from the node list. Filing them by who WRITES them, rather
than by what they are, is what let the old `targets/static` mount and the
`DATA_ROOT` variable that kept it distinct both go away.

## Where things land

One directory: `/docker/spark-dash-homegrown/central/`, a clone of this repo.
Config and runtime state share it, distinguished by `.gitignore` rather than by
path:

| What | Managed by | Tracked in git? |
|---|---|---|
| `compose.yaml`, `config/*.yml` | this repo | yes |
| `config/vllm-targets.yml` | hand-maintained | yes |
| `.env` | edited on the host | no — gitignored |
| `cluster/cluster.yml` | edited on the host | no — gitignored |
| `prometheus/` (TSDB), `alertmanager/`, `secrets/` | the containers, at runtime | no — gitignored |
| `targets/` | the backend, from `cluster.yml` | no — gitignored |

Config comes from git; data does not. Keeping data at a findable path rather
than in a named volume under `/var/lib/docker` is the point — you can `du` it,
`tar` it, and see it without `docker volume` commands.

> **`git clean -fdx` here would delete the Prometheus TSDB**, along with
> `.env`, `cluster.yml` and the `.env.bak-*` files — everything gitignored.
> Plain `git clean -fd` will not: only `-x` removes ignored files. This is the
> one real cost of keeping data inside the working tree, and it is worth
> knowing before you reach for that command to clean up a build.

> **Every runtime directory is gitignored, not merely untracked.** That is
> deliberate: `git add -A` in a deployed stack would otherwise stage the entire
> TSDB, and `secrets/` holds the ntfy topic URL that is deliberately kept out
> of git.

The main repo — needed for `publish-images.sh` and the validation scripts —
follows the usual convention:

```bash
REPO=/docker/spark-dash-homegrown
git clone https://forgejo.indielab.tech/brian/spark-dash-homegrown.git "$REPO" || git -C "$REPO" pull
```

### Two target directories

| Path in container | Source | Contents |
|---|---|---|
| `targets/generated/` | Docker volume, written by the backend | `agents.yml`, `node-exporters.yml` — from `cluster.yml` |
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
