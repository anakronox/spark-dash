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

Alerts are delivered by webhook to ntfy, which has a built-in `alertmanager`
template that renders the payload as a readable notification — no bridge
container involved.

The topic URL is **not** in git. An ntfy topic is a capability: anyone holding
the URL can both publish to it and subscribe to your alerts, and this repo is
public. So it's read from a file on the host:

```bash
sudo mkdir -p /docker/spark-dash-stack-central/secrets

# Pick an unguessable topic name — the URL is the only access control there is.
echo -n 'https://ntfy.sh/spark-dash-<something-random>?template=alertmanager' \
  | sudo tee /docker/spark-dash-stack-central/secrets/ntfy-url

sudo chown -R 65534:65534 /docker/spark-dash-stack-central/secrets
sudo chmod 600 /docker/spark-dash-stack-central/secrets/ntfy-url

docker compose up -d alertmanager
```

`echo -n` matters: a trailing newline becomes part of the URL and the POST
fails.

`?template=alertmanager` is what makes the notification readable — without it
you get the raw webhook JSON on your lock screen.

Then subscribe to the same topic in the ntfy app, and test it end to end:

```bash
# Should arrive on your phone within a few seconds.
curl -d "spark-dash test" "$(sudo cat /docker/spark-dash-stack-central/secrets/ntfy-url)"

# Confirm Alertmanager loaded the receiver.
curl -s localhost:9093/api/v2/status | jq '.config.original' | grep -c url_file
```

Critical alerts use the same topic but are delivered faster (10s rather than
45s batching) and repeat every 4h rather than 12h.

If you'd rather self-host ntfy, only the hostname in that file changes.

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

Two locations, with different lifecycles:

| Path | What | Managed by |
|---|---|---|
| `/docker/hawser/spark-dash-stack-central/` | `compose.yaml`, `.env`, `prometheus.yml`, `targets/static/` | hawser, synced from git |
| `/docker/spark-dash-stack-central/` | Prometheus TSDB, generated scrape targets | the containers, at runtime |

Config comes from git; data does not. Keeping them apart means a stack re-sync
can never touch your history, and the data path is somewhere you can actually
find and back up — rather than a named volume under `/var/lib/docker`.

`DATA_ROOT` in `.env` controls the second path.

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
