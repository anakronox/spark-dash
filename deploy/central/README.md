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
git clone https://forgejo.indielab.tech/brian/spark-dash-homegrown.git
cd spark-dash-homegrown
docker login forgejo.indielab.tech      # Forgejo token with package write
./scripts/publish-images.sh backend
```

## Configure

```bash
cp .env.example .env
$EDITOR .env          # set SPARK_NODES
```

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

## Verify

```bash
# Backend liveness and self-assessment (this is what UptimeKuma watches).
curl -s localhost:8080/health | jq

# What the backend thinks the cluster is.
curl -s localhost:8080/api/nodes | jq

# Confirm the rendered scrape targets look right.
docker compose exec backend cat /etc/prometheus/targets/agents.yml

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
