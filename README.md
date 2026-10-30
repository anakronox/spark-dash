# spark-dash

A self-hosted dashboard for NVIDIA GB10 machines (ASUS GX10, DGX Spark), from
one node to a home cluster. GPU and system health, temperatures, network, and
live metrics for the LLMs running on them: llama.cpp router, vLLM, SGLang and
TensorFold.

![The spark-dash dashboard in compact view, solarized theme: a pooled two-node cluster and a standalone node in their own frames, beside the Clients card with its callers' requests and tokens; System Activity as gauges, a dial per node for GPU utilisation, GPU temperature and memory used; Temperatures as gauges, processor, GPU and network averaged per node, each dial marked where it would turn amber and red; System Activity charting a metric per chart and a line per node; Network Activity charting RoCE and management traffic, received above the line and sent below; and the GPU process and model tables](Screenshot.jpg)

## What you get

- **GB10 read correctly.** Unified memory from the kernel, not NVML's flat
  line; throttle state; memory pressure. Every threshold is measured on this
  hardware: a GX10 sits at ~84°C doing routine work, so a generic 80°C alert
  would page you all day.
- **Which process holds the memory**, labelled by runtime: "97 GiB used"
  becomes "97 GiB held by these two vLLM shards".
- **Per-model inference state:** loaded and sleeping models, slots, KV cache,
  decode and prefill counted separately, time to first token.
- **Pooled clusters treated as one machine**, with free memory summed only
  where a single model could use it.
- **Every sensor and every link:** 18–23 temperature sensors per node, each
  against its own limit, and every interface and RoCE port with its negotiated
  rate, errors and drops.
- **35 alert rules** with push notifications to ntfy, Slack, Discord or
  Telegram, set up from the UI.
- **History for 180 days** in Prometheus, live updates every 1–2 seconds, and a
  layout you arrange yourself: move, size and copy any card, nine themes.
- **Read-only.** It can't load, unload or kill anything.

Optional, each off until you switch it on: one-button DGX OS updates, who is
calling your models (via LiteLLM), questions answered by your own model,
scheduled reports, and MCP tools for agents such as Claude Code.

**[All features in detail →](docs/features.md)**

## Quickstart

Two Docker Compose stacks: `central/` on a monitoring host, `node/` on each
GB10. Usually those are different machines
([why](docs/deployment.md#why-not-on-a-gx10)); with only one GB10, put both on
it ([single host](docs/deployment.md#single-host--everything-on-one-gb10)).

You need Docker with Compose v2, plus the NVIDIA Container Toolkit on each
node. Images build on the hosts that run them, so no registry account.

### 1. Monitoring host

```bash
git clone https://github.com/anakronox/spark-dash.git && cd spark-dash
./scripts/build-images.sh backend

cd central
cp .env.example .env
$EDITOR .env                    # set ALERTMANAGER_EXTERNAL_URL

mkdir -p cluster prometheus targets alertmanager secrets
cp cluster.yml.example cluster/cluster.yml
$EDITOR cluster/cluster.yml     # your nodes (see below)

# The containers run as non-root and need to own their directories.
sudo chown 65534:65534 prometheus alertmanager secrets
sudo chown 10002:10002 targets
sudo chmod 2770 secrets

docker compose up -d
```

The dashboard is on `:8080`. A minimal `cluster.yml`:

```yaml
# central/cluster/cluster.yml
nodes:
  - id: node-1                 # becomes the `node` label on every metric
    host: 10.0.0.11
    runtimes:
      llama_routers:
        - port: 8001
          scrape_metrics: true # per-model tokens/sec and KV cache
      vllm: [8120]

  - id: node-2
    host: 10.0.0.12
    cluster: alpha             # pools memory with other `alpha` nodes
```

You can also edit it later from **Settings → Nodes**, with no restart
([more](docs/deployment.md#adding-a-node-or-an-engine-on-one)).

### 2. Each GB10 node

```bash
git clone https://github.com/anakronox/spark-dash.git && cd spark-dash
./scripts/build-images.sh agent

cd node
cp .env.example .env
$EDITOR .env                    # BACKEND_URL=http://<monitoring-host>:8080
docker compose up -d
```

`BACKEND_URL` is the monitoring host, the same on every node. The agent fetches
its config from there. To check it worked:

```bash
curl -s localhost:9500/snapshot | jq .config.source
```

| | |
|---|---|
| `central` | Working. |
| `unreachable` | `BACKEND_URL` is wrong, or a firewall is in the way. |
| `env` | The backend answered, but this node isn't in `cluster.yml` yet. |

### 3. Open the dashboard

**Get started** lists what's left to set up and ticks each step off only once
it works: every node reporting, Prometheus scraping, an alert that reaches your
phone. That's the whole install.

## Optional features

| Feature | Needs | Setup |
|---|---|---|
| DGX OS updates | An SSH key and a sudo login on each Spark | [Quick setup](docs/fleet-updates-setup.md) |
| Client stats | A LiteLLM proxy in front of your engines | [Client stats](docs/deployment.md#client-stats--optional) |
| Ask the dashboard | Any OpenAI-compatible model | [Asking the dashboard](docs/deployment.md#asking-the-dashboard--optional) |
| Scheduled reports | Nothing | [Reports](docs/deployment.md#reports--scheduled-and-on-demand) |
| Tools for agents (MCP) | Nothing; switch on in Settings | [Tools for agents](docs/deployment.md#tools-for-agents-mcp--optional) |

## Docs

- [Features](docs/features.md): everything it shows and does.
- [Deployment](docs/deployment.md): the stacks, the cluster file, the optional
  features in full, and building images for a registry.
- [Metrics](docs/metrics.md): what's collected, and every `sparkdash_*` series.
  Prometheus holds it all, so [Grafana](central/grafana/) or your own views
  work too.
- [Architecture](docs/architecture.md) and [app design](docs/app-design.md):
  how it's built, and the API.
- [Roadmap](docs/roadmap.md): the decision log, including what was
  deliberately not built.
- [DGX OS updates: the research](docs/fleet-updates.md): how NVIDIA updates
  GB10 systems.
- [Working on spark-dash](AGENTS.md): for people and agents changing the code.

Bugs and questions go in GitHub Issues. Most of this has been measured on
three GB10s on one LAN, so a report from different hardware is especially
useful.

## License

[MIT](LICENSE).
