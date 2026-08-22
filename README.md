# spark-dash

A scalable web dashboard for a home cluster of NVIDIA GB10-based inferencing
servers (ASUS GX10 / "DGX Spark" class hardware), showing GPU/system health and
live metrics for the LLM inferencing jobs (llama.cpp router + vLLM) running on
them.

Supports single node and clusters.

![The spark-dash dashboard: a pooled two-node cluster and a standalone node, GPU/CPU/memory history, the GPU process table, and per-model throughput and state](Screenshot.png)

Three GB10s — two pooled into a tensor-parallel cluster, one standalone. Cards
are draggable and can be paired side by side; every threshold shown is measured
for this hardware rather than guessed.

## What it monitors

**77 metric families per node**, live at 1–2s over a WebSocket and kept for 180
days in Prometheus. Read-only by design: it can't load, unload or kill anything.

**The GPU, as GB10 actually reports it.**

- Utilization, temperature, power, clocks.
- **Unified memory done correctly.** `nvmlDeviceGetMemoryInfo` reports
  `total ≈ MemTotal` on GB10 no matter what is resident, so the number every
  generic exporter shows is useless here. The agent reads `/proc/meminfo`
  instead, which is accurate under load.
- **A load-gated throttle state machine** — `IDLE` / `PASS` / `LOCKED` /
  `THROTTLED`. A low clock only means something while the GPU is busy, and on
  GB10 a throttle usually means power delivery rather than heat.
- **Memory pressure (PSI)**, which catches contention *before* swap or a freeze.
  Percent-used does not.

**Who is actually holding the memory.** Per-process GPU attribution, each
process labelled with the runtime that owns it — llama.cpp, vLLM, SGLang,
ComfyUI — so "97 GiB used" becomes "97 GiB held by these two vLLM shards". On a
shared pool that difference is the whole question.

**Per-model inference state.**

- llama.cpp router mode: which models are loaded, sleeping or unloaded, slots in
  use, KV-cache occupancy, and a timeline of every load and unload.
- vLLM and SGLang scraped *directly* by Prometheus, so the full upstream surface
  — latency histograms, per-request counters — is stored whether or not this UI
  draws it.
- **Decode and prefill are separate numbers.** Added together they read 47,672
  tok/s while a model generates 48, because a large prompt landing inside one
  poll window is a real ingest rate and is not what throughput means.

**The cluster as one machine.** Nodes given a `cluster:` name pool their memory,
so free space is summed *within* a cluster and never across — a fleet-wide total
would describe capacity that cannot hold a model. Tensor-parallel workers are
recognised as shards of one job rather than counted as separate models, and
`ClusterNodeClockLagging` / `ClusterNodeRunningHot` compare members against each
other to find the straggler that sets the pace.

**The fabric.** Every interface and RoCE port: link state, negotiated rate,
throughput, errors and drops. Ports you have deliberately unplugged are excluded
by name from alerting — and the RoCE device behind an interface goes with it,
since one cable carries both.

**Alerting that has been calibrated rather than guessed.** 34 rules, push
notifications via [ntfy](https://ntfy.sh) with no account or API key. Thermal
bands come from the hardware's own limits per node, and an alert fires when a
node falls back to a guessed one — a GX10 sits at ~84°C during routine work, so
a generic 80°C line would page you constantly.

**History worth keeping.** 15 chartable metrics — GPU and CPU utilization,
clocks, temperature, power, memory, all four PSI signals, swap and disk I/O,
throughput and prefill — over 1h to 7d, per node or aggregated.

## Quickstart

Everything runs in Docker; nothing is installed on the base OS. There are two
stacks — `central/` on a monitoring host, and `node/` on each GB10 you want to
watch. By default those are different machines, so that losing a node does not
also lose the history explaining why
([why](docs/deployment.md#why-not-on-a-gx10)). If you have no second machine,
[single-host](docs/deployment.md#single-host--everything-on-one-gb10) puts both
on one GB10 and states what that costs.

You need Docker with Compose v2 on both, plus the NVIDIA Container Toolkit on
each node — the agent reads NVML. **No registry account and no `docker login`:
the images are built on the hosts that run them.**

### 1. Monitoring host

```bash
git clone <this repo's URL> spark-dash && cd spark-dash
./scripts/build-images.sh backend               # builds spark-dash-backend:latest

cd central
cp .env.example .env
$EDITOR .env                    # one value to change: ALERTMANAGER_EXTERNAL_URL

mkdir -p cluster prometheus targets alertmanager secrets
cp cluster.yml.example cluster/cluster.yml
$EDITOR cluster/cluster.yml     # your nodes: id, host, and what each one serves

# Required, not tidiness. Docker creates missing bind-mount sources owned by
# root and both containers run non-root; skip this and Prometheus crash-loops
# with a message that reads like a Prometheus bug.
sudo chown 65534:65534 prometheus alertmanager
sudo chown 10002:10002 targets

docker compose up -d
```

The dashboard is on `:8080`.

### 2. Each GB10 node

Build on the node itself — it is arm64, which avoids cross-building entirely:

```bash
git clone <this repo's URL> spark-dash && cd spark-dash
./scripts/build-images.sh agent                 # builds spark-dash-agent:latest

cd node
cp .env.example .env
$EDITOR .env                    # one value to change: BACKEND_URL
docker compose up -d
```

`BACKEND_URL` is genuinely the only edit, and the same `.env` then deploys
unchanged to every node: the agent takes its id from the host's hostname, and
takes which routers and vLLM endpoints to watch from `cluster.yml` on the
monitoring host rather than from its own config.

### 3. Check it

```bash
curl -s <monitoring-host>:8080/health | jq '{status, problems}'
```

`ok` with an empty `problems` means every node in `cluster.yml` is being
scraped. A node listed there but not yet running its agent appears in
`problems`, which is the intended way to notice a half-finished deploy.

### 4. Adding a node, or an engine on one

**One file defines the cluster** — `central/cluster/cluster.yml` — and there are
two ways to edit it. Both write the same file, and **neither needs a restart**:
the backend re-reads it on a timer, and it renders Prometheus's scrape targets
from the same entry, so there is no second inventory to keep in sync.

**From the dashboard.** ⚙ **settings** → **Cluster**:

- **`+ node`** adds a row. Set its **host** — the node's LAN address — and
  optionally a **cluster** name.
- **`+ router`** adds a llama.cpp router by **port**. The **metrics** tick beside
  it opts that router in to `/metrics?model=`, which is what provides per-model
  tokens/sec and KV-cache detail.
- **`+ vLLM`** and **`+ SGLang`** add those engines the same way, by port.
- **copy yaml** shows the YAML for that node if you would rather paste it
  somewhere than save from here.

The panel also lists the interfaces each node is actually reporting, as tick
boxes, so excluding a deliberately-unplugged port from alerting does not mean
looking its name up.

**Or edit the file.** Same result:

```yaml
# central/cluster/cluster.yml
nodes:
  - id: node-1                 # becomes the `node` label on every metric
    host: 10.0.0.11
    runtimes:
      llama_routers:
        - port: 8001
          scrape_metrics: true # per-model tokens/sec and KV cache
        - port: 8108
      vllm: [8120]
      sglang: [30000]          # only if started with --enable-metrics

  - id: node-2
    host: 10.0.0.12
    cluster: alpha             # pools memory with other `alpha` nodes
```

**Runtimes are ports, not URLs.** They are resolved against that node's own
host, so a node's address appears exactly once — which is what lets the node
stack in step 2 deploy byte-identical to every machine. A node that only holds
weights for a tensor-parallel cluster needs no `runtimes:` block at all.

**A new node still needs step 2 run on it.** Listing it here is what makes the
dashboard and Prometheus look for it; the agent is what answers.

**`cluster:` is not cosmetic.** Clustered nodes pool memory for distributed
inference, so their free space is summed together and treated as capacity a
single model could occupy. Unclustered nodes are never summed with anything —
a fleet-wide total would describe memory that cannot hold a model.

Deploying from a registry instead of building on each host is the maintainer
path — see [building and shipping
images](docs/deployment.md#building-and-shipping-images). It needs
`PULL_POLICY=always` in each `.env`, for reasons the compose files spell out.

## Docs

- [Requirements](docs/requirements.md) — goals, current stack, functional/
  non-functional requirements.
- [Architecture](docs/architecture.md) — Prometheus for collection/storage,
  homegrown backend + frontend for the UI; component diagram; scaling approach.
- [Metrics catalog](docs/metrics.md) — exactly what's being collected, from
  vLLM, llama.cpp router mode, and GPU/system exporters, including GB10-specific
  caveats (unified memory, router autoload behavior).
- [Roadmap](docs/roadmap.md) — the decision log: every workstream, what shipped,
  and what was deliberately not built, with the reasoning kept.
- [App design](docs/app-design.md) — backend/frontend stack (FastAPI + Svelte 5),
  API surface, live-update contract, and panel/visual design rules.
- [Deployment](docs/deployment.md) — Docker-only deployment approach (base OS
  stays untouched) and the per-node/central Compose service breakdown.

## Bring your own dashboard

Prometheus holds everything the UI shows and 180 days of it, so building your
own views instead is a supported path rather than a workaround — the inference
engines are scraped *directly*, so latency histograms and per-request counters
the bundled frontend never renders are already stored.
[central/grafana/](central/grafana/) has an importable starter dashboard and,
more usefully, the list of ways this particular metric surface will mislead you.

[docs/roadmap.md](docs/roadmap.md) is the authoritative task list. Issue
tracking mirrors it on a LAN-internal Forgejo instance that is not reachable
from outside the network, so the roadmap file is the copy to read.

## License

[MIT](LICENSE).
