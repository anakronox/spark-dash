# Grafana

A starter dashboard for anyone who would rather build their own views than use
the bundled frontend. Nothing here is required — the dashboard and the alerts
work without it.

```
Grafana → add a Prometheus data source → http://<monitoring-host>:9090
       → Dashboards → New → Import → upload spark-dash-overview.json
       → pick that data source when prompted
```

The import asks for a data source because the JSON declares one as an input
rather than hardcoding a uid, so it lands in any Grafana without editing.

**This repo ships no Grafana container.** Point an existing one at Prometheus;
:9090 is published on the monitoring host. Adding a service to
`central/compose.yaml` would also need re-applying by hand to the deploy copy of
that file, which is maintained separately and deliberately.

## What is available to query

| source | metric names | notes |
|---|---|---|
| `sparkdash_*` | 73 | the agent: GPU, unified memory, PSI, clocks, thermals, models, fabric |
| `node_*` | ~314 | stock node_exporter, one job per node plus the monitoring host |
| `vllm:*` / `sglang:*` | ~106 | scraped **directly**, so the full upstream surface is here |

The engines are scraped by Prometheus rather than proxied through the agent, so
metrics the bundled frontend never renders — `vllm:time_to_first_token_seconds`,
`vllm:e2e_request_latency_seconds`, per-request success and failure counters —
are already stored and queryable. A Grafana user has strictly more to work with
than the frontend shows.

Retention is 180 days.

## Things that will bite you

**Some `_total` series are typed as gauges.** `sparkdash_network_receive_bytes_total`
and its siblings are monotonic counters read from sysfs, but the agent exports
them through a gauge family, so Grafana will not suggest `rate()` and PromQL
linters may object. Using `rate()` on them is correct — they are real counters,
and they reset only on host reboot, which `rate()` already handles.

**llama.cpp throughput is a gauge, not a counter.**
`sparkdash_llama_model_tokens_per_second` is a rate the agent computed over its
own ~1s poll and then published, sampled at Prometheus's 15s. You cannot
recompute it over a window of your choosing. vLLM and SGLang publish real token
counters and are scraped natively, so for those engines you can.

**`cluster` is present only on nodes that have one.** Clustered nodes pool
memory for distributed inference; standalone ones do not. A standalone node
carries no `cluster` label at all, so `sum by (cluster)` files it under an empty
group rather than under a name. This is deliberate — summing free memory across
clusters would report capacity that does not exist — but it surprises people.

**Never sum a memory pool across nodes.** GB10 has no separate VRAM: one
coherent pool per node serves the models, every other GPU workload and the OS.
Adding three nodes' pools together describes a single 384 GB space that nobody
can allocate from. The "what is eating the pool" panel repeats per node for
exactly this reason, and any panel you add over `sparkdash_memory_*` or
`sparkdash_gpu_process_memory_bytes` should do the same.

**Throughput is one sum over a name regex, not a sum per engine added
together.** `sum(A) + sum(B)` looks equivalent and is not: binary `+` between
instant vectors keeps only label sets present on **both** sides, so a node
running only one engine contributes nothing and charts as flat zero while it
serves. This was a live bug here. Select the families by `__name__` and sum
once:

```promql
sum by (node) ({__name__=~"sparkdash_(llama_model|vllm|sglang)_tokens_per_second"})
```

**States are one series per state, not an encoded number.**
`sparkdash_node_health{state="good"}` is 1 or 0, and the same shape holds for
`gpu_clock_state`, `psi_memory_state` and `llama_model_state`. Filter on the
label; there is no enum to decode.

**Thresholds are metrics.** `sparkdash_gpu_temp_warning_celsius` and
`_critical_celsius` are derived per node from NVML's own slowdown threshold, so
a panel can draw its bands from the hardware instead of hardcoding 82/86. The
GPU temperature panel plots them as dashed reference lines.

**A distributed model's worker shards are unattributed here.** The
`sparkdash_gpu_process_*` series come from each agent's own `/metrics`, and a
tensor-parallel worker has no endpoint to learn its model name from — so on a
cluster like `danflashes` the head node's shard carries
`model="deepseek-v4-flash-0731"` and the worker's identical 96.8 GiB carries
`model=""`. Summing by model therefore under-reports a distributed model's
footprint by however many workers it has. The live dashboard fills this in from
the cluster's head node (see `attribute_cluster_shards`); Prometheus does not,
because the name is live data and the agent's config has a 60s TTL. To get the
true footprint here, sum by cluster and runtime rather than by model.

**GPU process memory is aggregated, never per-pid.** It carries `runtime` and
`model`, not `pid` — a deliberate cardinality trade. Per-process detail with
pids exists only in the agent's live `/snapshot`, and is not in Prometheus at
all.

**`sparkdash_gpu_process_memory_bytes` carries an empty `model` for some rows,
and that is not missing data.** Series are partitioned by
`(runtime, model, server)`, so a router parent holding only its own overhead
appears as `model=""` beside its children's per-model rows. The partition is
disjoint — verified against a live node, hand-summed against
`sum by (node)` — so aggregating at any level is safe and nothing
double-counts. `runtime=""` likewise means a process nothing recognised, which
is a category worth seeing rather than a gap.

## The dashboard

Six sections, `$node` filters all of them:

- **Cluster** — nodes up, nodes not healthy, throughput, models resident,
  watched links down, unmonitored runtimes
- **GPU** — utilization, temperature against its derived bands, SM clock against
  the applications clock, power
- **Unified memory** — used, PSI some/full, and pool composition by workload
  (repeated per node)
- **Inference** — tokens/sec by model, requests running and waiting, KV cache,
  model states
- **Fabric** — throughput in bits, and an interface table showing `up` beside
  `monitored`
- **Agent health** (collapsed) — snapshot age, collection duration, build sha
  per node, collector errors

Every panel carries a description explaining what it means and where it misleads;
hover the ⓘ. Between them they double as the catalog `docs/metrics.md` does not
yet contain for the `sparkdash_*` surface.

Panel queries were run against a live Prometheus before shipping. Grafana's
**transformations** — the merges and column renames in the two multi-query
tables — were not, since that needs Grafana itself. If the Interfaces table
renders one row per query instead of one per interface, that is the merge, and
the fix is in its transformation list.
