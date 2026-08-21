# Metrics Catalog

What we actually want to scrape/collect, and from where. This is the source of truth
for exporter/scrape config once we start implementing.

## 1. GPU / system-level (per node)

Collected by **`spark-dash-agent`** (ours to build) — one container per node,
reading NVML directly plus the GB10-specific signals that nothing generic
exposes correctly. Third-party GPU exporters are deferred to Phase 4 (see
[below](#baseline-exporter--deferred-to-phase-4)).
The approach follows hard lessons already documented by
[`sparkview`](https://github.com/parallelArchitect/sparkview) — a GB10-aware TUI
monitor (see [related tools](https://github.com/parallelArchitect) — same author
also publishes `nvml-unified-shim`, `spark-gpu-throttle-check`, and
`cuda-unified-memory-analyzer`) — and worth treating as the reference
implementation rather than re-deriving these from scratch.

### Baseline exporter — deferred to Phase 4

[`dcgm-exporter`](https://github.com/NVIDIA/dcgm-exporter) and
[`dgx-spark-prometheus`](https://github.com/ateska/dgx-spark-prometheus) are
**not** in the Phase 1 stack. The agent reads NVML directly for
utilization/temp/power/clocks, and `dcgm-exporter`'s headline advantage (GPU
memory) is exactly the number unified memory breaks on GB10. What DCGM would
still add is deep profiling telemetry — SM/tensor-core activity, memory
bandwidth — genuinely useful for inference tuning, but not MVP-critical and of
uncertain GB10 support. Revisit in Phase 4.

`nvidia-smi` remains fine for ad hoc checks, but isn't a metrics source for the
dashboard on its own.

### `spark-dash-agent` collectors

Modeled directly on `sparkview`'s validated techniques:

- **`gpu` — NVML via `nvitop`:** utilization, temperature, power, clock
  frequency. Standard NVML reads, no GB10 caveat.
- **`memory` — unified memory, done correctly:** `nvmlDeviceGetMemoryInfo` on
  GB10 reports `total ≈ MemTotal` regardless of actual usage — not useful. Use
  `vm.total - vm.available` (`/proc/meminfo` `MemTotal`/`MemAvailable`, or
  `psutil.virtual_memory()`) for used memory instead, with `vm.total` as the
  display total. This is accurate under heavy inference load.
  ([`nvml-unified-shim`](https://github.com/parallelArchitect/nvml-unified-shim)
  does the same fix at the NVML layer — relevant only if a third-party exporter
  is adopted in Phase 4.)
- **`psi` — memory pressure:** `/proc/pressure/memory` gives a LOW/MOD/HIGH/CRITICAL
  signal that catches contention *before* swap or a system freeze — raw
  percent-used doesn't. Worth its own gauge/alert, not just folded into the
  memory-used number.
- **`clock` — throttle state:** a load-gated state machine —
  `IDLE` (not under load, not evaluated) / `PASS` (healthy under load) /
  `LOCKED` (externally capped via `nvidia-smi -lgc`) / `THROTTLED` (low clock
  under load — power-delivery issue suspected). sparkview's field-derived
  threshold: healthy sustained load runs ~2400MHz; **clock < 1400MHz under
  sustained load → THROTTLED**, degraded systems have been observed in the
  500-850MHz range. This is a real "something's wrong with this node" signal
  that neither NVML nor DCGM surfaces on its own.
- **Non-LLM GPU workloads share the pool.** The GX10 also runs ComfyUI
  (image generation) alongside the inference runtimes, and on GB10 that
  competes for the *same* unified memory the models need — there's no separate
  VRAM to isolate it. The process table therefore labels GPU workloads
  generally, not just inference servers, so "12GB used with no models loaded"
  has a visible answer. `LLM_RUNTIMES` in the agent separates the two
  categories for the UI.

  Identification needs three signals, because process names are frequently
  useless: vLLM and ComfyUI both run as bare `python`, distinguishable only by
  command line and working directory respectively.
- **Per-process GPU memory attribution** (part of `gpu`) — `nvidia-smi --query-compute-apps`
  (GPU UUID, PID, process name, used memory), for tying usage back to a
  specific llama.cpp/vLLM container and for the process-list panel (see
  [architecture.md](architecture.md#live-view-fast-path)).

**Out of scope: GB10 power rails via `spark_hwmon`.** This would have added
`gpu`, `dc_input`, `syspl1`, `PROCHOT`, power-limit level, and `Tj-rise`
telemetry, but [`spark_hwmon`](https://github.com/antheas/spark_hwmon) is a
real ACPI-binding kernel driver (`dkms`, kernel headers, possibly Secure Boot
MOK signing) — a genuine host kernel modification with no container-based
workaround. Deliberately descoped to keep the base OS untouched (see
[deployment.md](deployment.md)); revisit only if that priority changes.

**Known GB10 caveat — time-slicing:** if GPU time-slicing is ever used (e.g.
under k8s with `KUBERNETES_VIRTUAL_GPUS=true`), `dcgm-exporter` (should it be
adopted in Phase 4) reports
utilization/power/temperature as identical across all virtual devices — i.e. it
can't currently distinguish per-slice load on Blackwell GB10. Not relevant to
the Docker Compose setup today, but worth remembering if the
[roadmap](roadmap.md) ever moves to k8s-based scheduling.

**Metrics to collect:**

- GPU utilization %
- Memory used / available — via the agent's UMA-correct calc, not raw NVML
- Memory pressure (PSI) state
- Clock state (IDLE/PASS/LOCKED/THROTTLED)
- Temperature (GPU + CPU, current and session peak)
- Power draw (from NVML — no `spark_hwmon` power-rail detail; see
  out-of-scope note above)
- Per-process GPU memory attribution (for tying usage back to a specific
  llama.cpp/vLLM container)
- Host-level: CPU load, system memory, disk usage, network throughput
  (via `node_exporter` — standard, ARM64-supported, no GB10-specific issues)
- Node liveness (is the scrape target even up)

## 2. vLLM (per model/instance)

vLLM ships a native Prometheus endpoint — no separate exporter needed.

- Endpoint: `http://<host>:<port>/metrics`
- Metric prefix: `vllm:*`

Key metrics to surface on the dashboard:

- `vllm:num_requests_running`, `vllm:num_requests_waiting` — in-flight vs. queued
- `vllm:kv_cache_usage_perc` — KV cache pressure (0-1)
- `vllm:prompt_tokens_total`, `vllm:generation_tokens_total` (→ derive tokens/sec)

  **Derive them SEPARATELY.** Adding the two rates together is what the agent
  originally did, and it made the reported figure meaningless: measured on the
  live cluster 2026-08-21, the combined number hit 47,672 tok/s while
  generation peaked at 47.9/s. Prefill and decode differ by orders of magnitude
  and answer different questions -- how fast a request is *accepted* against
  how fast it is *answered*. llama.cpp has the identical trap
  (`tokens_predicted_total` + `prompt_tokens_total`), so both are now reported
  as separate series and the combined one is kept only so recorded history is
  not orphaned.
- `vllm:time_to_first_token_seconds` (histogram)
- `vllm:e2e_request_latency_seconds` (histogram)
- `vllm:request_success_total` / failure counters
- GPU cache block eviction / prefix-cache hit rate, if we want cache-efficiency
  panels later

### SGLang (per instance) — same shape, different names

SGLang also ships a native Prometheus endpoint, so it is scraped directly and
collected by the same code path as vLLM. The only differences are names.

- Endpoint: `http://<host>:<port>/metrics`, **only with `--enable-metrics`**.
  Without the flag there is nothing to scrape, and the instance reports as
  configured-but-unreachable — the right answer, and a confusing one if the
  missing flag is what you are looking for.
- Metric prefix: `sglang:*`; the examples in its docs use port 30000.

| SGLang | maps to | vLLM's name for it |
|---|---|---|
| `sglang:num_running_reqs` | requests running | `vllm:num_requests_running` |
| `sglang:num_queue_reqs` | requests waiting | `vllm:num_requests_waiting` |
| `sglang:prompt_tokens_total` | → tokens/sec | `vllm:prompt_tokens_total` |
| `sglang:generation_tokens_total` | → tokens/sec | `vllm:generation_tokens_total` |
| `sglang:cache_hit_rate` | **nothing — see below** | — |

**`sglang:cache_hit_rate` is NOT `vllm:kv_cache_usage_perc`.** One is the
fraction of prompt tokens served from the *prefix cache* — how much work was
skipped; the other is how full the KV cache is. Same 0-1 shape, different
question. Rendered in the KV column it would show a number that reads as
occupancy: 93% prefix hits is a node doing well, 93% occupancy is a node about
to evict. So the KV cell is **empty** on an SGLang row. `sglang:token_usage` is
the closer analogue and is a candidate once it can be checked against a running
server.

**`sglang:gen_throughput` is not used either**, for the same class of reason:
it is instantaneous *decode* throughput, while every other runtime contributes
prompt+generation over the poll interval, and the node card sums them. It is
the fallback only when the token counters are absent from a scrape.

## 3. llama.cpp (router mode, per node)

`llama-server` exposes a Prometheus-compatible endpoint via the `--metrics` flag.
In **router mode** this gets more nuanced:

- Per-model metrics are fetched as `GET /metrics?model=<name>` (and `/props?model=`
  for model state/info) — there is **no built-in aggregated "all models on this
  router" endpoint** as of this writing
  ([feature request, open](https://github.com/ggml-org/llama.cpp/discussions/19197)).
  Our exporter/collector will need to enumerate loaded models (via the router's
  model list) and fan out a request per model itself.
- **Known bug to design around:** `GET /metrics?model=X` currently triggers
  autoload of that model and resets its idle-sleep timer
  ([issue #23096](https://github.com/ggml-org/llama.cpp/issues/23096)). A naive
  Prometheus scrape loop would therefore *prevent evicted models from ever
  staying evicted* — actively fighting the router's LRU memory management. We
  must not blindly scrape every known model on every interval; only scrape
  models currently reported as loaded (via `/props` or the router's own
  status/list endpoint, which should be safe to poll), and treat "is it loaded"
  itself as a metric rather than something we probe for by requesting metrics.

Metrics to surface:

- Which models are currently loaded vs. evicted, per node
- Recent load/swap events and their duration (a swap is a user-visible latency
  spike — worth a dedicated "router activity" panel/log, not just a gauge)
- Per-loaded-model: requests in flight, tokens/sec, KV cache usage, latency
  (same shape as vLLM where available — llama.cpp's server metrics are modeled
  similarly)
- Slot usage (`/slots` endpoint) — concurrent request slots in use vs. available

## What the agent itself exports

Everything above describes what the ENGINES and exporters publish upstream.
This section is the other half: the `sparkdash_*` series the agent republishes,
which is what alert rules, the history charts and any Grafana dashboard
actually query. **85 metric names**, every one carrying a `node` label, and a
`cluster` label on nodes that belong to one.

Read [central/grafana/README.md](../central/grafana/README.md) alongside this —
it lists the ways this surface misleads, which a table of names cannot convey.

`tests/test_metrics_catalog.py` asserts this list and the exporter agree in both
directions, by rendering a snapshot rather than trusting the prose.

### Node and agent

| metric | labels | meaning |
|---|---|---|
| `node_up` | | 1 if the agent produced a snapshot |
| `node_health` | `state` | one series per state; 1 for the active one |
| `agent_build_info` | `build` | always 1; the build sha rides as a label |
| `agent_snapshot_age_seconds` | | age of what is being served — climbing means collection is behind the poll |
| `agent_collect_duration_seconds` | | wall time of one collection |
| `agent_collections_total` | | collections that completed |
| `agent_collect_failures_total` | | collections that raised; the previous snapshot is kept |
| `agent_collection_stalled` | | 1 while readers are getting stale data rather than waiting briefly |
| `collector_errors` | `collector` | 1 per collector that failed this scrape |
| `unmonitored_runtime` | `runtime` | a runtime is running with nothing configured to collect it, **anywhere in its cluster** |

### GPU

| metric | labels | meaning |
|---|---|---|
| `gpu_utilization_percent` | | fraction of time a kernel was resident — busy, not efficient |
| `gpu_temperature_celsius` | | |
| `gpu_power_watts` | | whole-module draw on GB10, so it moves with CPU work too |
| `gpu_clock_mhz` | | SM clock |
| `gpu_clock_target_mhz` | | NVML applications clock — the reference throttling is judged against, **not** the boost ceiling |
| `gpu_clock_state` | `state` | `PASS` / `THROTTLED` / `LOCKED` / `IDLE`, load-gated |
| `gpu_temp_warning_celsius`, `gpu_temp_critical_celsius` | | the bands **as metrics**, derived per node from NVML's slowdown threshold |
| `cpu_temp_warning_celsius`, `cpu_temp_critical_celsius` | | same, from the ACPI critical trip |
| `temp_band_source_info` | `component`, `derived` | always 1; where each band came from |

### GPU processes

All five carry `runtime`, `model`, `server`. Aggregated by that key, **never
per-pid** — a deliberate cardinality trade, so per-process detail with pids
exists only in the agent's live `/snapshot`.

| metric | meaning |
|---|---|
| `gpu_process_memory_bytes` | GPU memory held, by workload |
| `gpu_process_count` | processes in that workload |
| `gpu_process_sm_percent` | share of SM time — answers "who is competing", not "what fraction of the device" |
| `gpu_process_encoder_percent`, `gpu_process_decoder_percent` | NVENC/NVDEC, separate fixed-function blocks |

An empty `model` is a router parent holding its own overhead, and an empty
`runtime` is a process nothing recognised. Both are categories, not gaps. The
partition is disjoint, so aggregating at any level is safe.

### Memory and pressure

| metric | meaning |
|---|---|
| `memory_total_bytes`, `memory_available_bytes`, `memory_used_bytes` | one pool; GB10 has no separate VRAM |
| `memory_unified` | 1 when CPU and GPU share one coherent pool |
| `memory_swap_used_bytes` | a LEVEL, not a flow — pages sit here indefinitely after a past squeeze |
| `psi_memory_some_avg10`, `psi_memory_some_avg60` | at least one task stalled on memory |
| `psi_memory_full_avg10`, `psi_memory_full_avg60` | EVERY runnable task stalled — the difference between working hard and stopped |
| `psi_memory_state` | `state` label: `LOW` / `MOD` / `HIGH` / `CRITICAL` |
| `cpu_utilization_percent`, `cpu_load1`, `cpu_temperature_celsius` | |
| `disk_total_bytes`, `disk_used_bytes`, `disk_available_bytes` | root filesystem only |

### Network and RDMA

| metric | labels | meaning |
|---|---|---|
| `network_up` | `interface` | 1 when the link is up |
| `network_monitored` | `interface` | 0 when excluded from alerting by `cluster.yml` — still collected and charted |
| `network_speed_mbps` | `interface` | absent while a link is down |
| `network_{receive,transmit}_bytes_total` | `interface` | monotonic counters **typed as gauges**; `rate()` is correct on them |
| `network_receive_errors_total`, `network_receive_dropped_total` | `interface` | |
| `network_transmit_errors_total`, `network_transmit_dropped_total` | `interface` | |
| `rdma_port_active` | `device`, `port` | |
| `rdma_port_monitored` | `device`, `port` | derived from the paired netdev — one cable carries both |
| `rdma_{receive,transmit}_bytes_total`, `rdma_errors_total` | `device`, `port` | |
| `rdma_port_info` | + `link_layer`, `rate` | always 1; the negotiated rate rides as a label because a string cannot be a gauge value |

### Inference — llama.cpp router mode

Per model: `model`, `router`. Per router: `router`.

| metric | meaning |
|---|---|
| `llama_router_up` | 1 when the router answered |
| `llama_models_known`, `llama_models_active`, `llama_models_sleeping` | registered / weights resident / slept but process alive |
| `llama_router_max_instances` | the `--models-max` ceiling |
| `llama_model_state` | `state` label; one series per state |
| `llama_model_size_bytes`, `llama_model_parameters`, `llama_model_context_length` | from the router's `meta`, absent on models it has never loaded |
| `llama_model_generation_tokens_per_second` | **decode — the throughput number** |
| `llama_model_prompt_tokens_per_second` | prefill |
| `llama_model_tokens_per_second` | the two added together; legacy, kept so history is not orphaned |
| `llama_model_kv_cache_percent`, `llama_model_requests_running`, `llama_model_requests_waiting` | active models only — emitting 0 for a sleeping one is indistinguishable from idle-but-loaded |

### Inference — engines (vLLM, SGLang)

One family per engine, `sparkdash_vllm_*` and `sparkdash_sglang_*`, each
labelled `model`. A family per engine rather than one with a `runtime` label,
because the vLLM names are what alert rules and stored history are written
against.

| metric | meaning |
|---|---|
| `{engine}_generation_tokens_per_second` | **decode — the throughput number** |
| `{engine}_prompt_tokens_per_second` | prefill |
| `{engine}_tokens_per_second` | the two added together; legacy |
| `{engine}_requests_running`, `{engine}_requests_waiting` | |
| `{engine}_kv_cache_percent` | **vLLM only.** The family exists for SGLang and never carries samples: SGLang publishes `cache_hit_rate`, which is prefix-cache hits rather than occupancy — a different question with the same 0–1 shape |
| `endpoint_reachable` | `runtime`, `endpoint` — 0 for a configured endpoint that did not answer |

### The three that will catch you out

1. **`_tokens_per_second` is prefill AND decode.** Measured on this cluster it
   read 47,672 tok/s while the model generated 48. Use the `generation_`
   variant.
2. **Never sum a memory pool across nodes.** No separate VRAM, no shared pool
   between boxes.
3. **`sum(A) + sum(B)` drops nodes running only one engine** — binary `+` keeps
   only label sets present on both sides. Select families by `__name__` regex
   and sum once.

## 4. Derived / cluster-level

Computed by the dashboard backend, not scraped directly:

- Cluster-wide GPU utilization / free capacity across all 3 nodes
- "What's running where" table: node × runtime × model × status
- Aggregate tokens/sec across the whole cluster
- Node health summary (up/down, last-seen)

### The `cluster` label, and why totals are usually wrong

Every scraped series carries `node`, and a **clustered** node also carries
`cluster`. The label is attached at scrape time from the `file_sd` target files
the backend renders out of `cluster.yml` — the agent can't supply it, because a
node has no way to know what it has been clustered with.

This exists because **memory sums within a cluster and never across clusters.**
Clustered nodes pool memory for distributed inference, so a model can span them
and their combined free space is real. Unclustered nodes can't do that, so
adding their free memory together describes capacity that does not exist.

```promql
# Free memory per cluster — the honest unit of capacity.
sum by (cluster) (
  sparkdash_memory_available_bytes
    * on(node) group_left(group) (max by (node, group) (up{job="spark-dash-agent"}))
)

# The question that actually matters: what is the largest model that fits?
# The best single cluster can offer — NOT the sum of all clusters.
max(
  sum by (cluster) (sparkdash_memory_available_bytes)
)
```

Two traps:

- **A standalone node carries no `cluster` label at all**, deliberately: an
  empty label would create a distinct series and a phantom cluster in
  aggregation. In PromQL that means `sum by (cluster)` buckets every standalone
  node together under the empty string. Where that matters, aggregate on `node`
  for the standalone case, which is what the backend's `cluster_key` does — a
  standalone node is a cluster of one.
- **The label is a name, never a count.** "pair" is wrong the moment a third
  node joins. It is also what you read in an alert, so it has to stand alone
  without a lookup table.
- **`sum` without `by (cluster)` is almost always the wrong answer.** It reads as
  cluster capacity and isn't.

## 5. Anomaly thresholds (starting point for Phase 3 alerting)

`sparkview`'s anomaly auto-logger already field-validated a set of trigger
conditions on GB10 hardware — reuse these as the initial Prometheus alerting
rules ([roadmap.md](roadmap.md) Phase 3) instead of guessing at thresholds from
scratch:

- Memory pressure (PSI) reaches MOD, HIGH, or CRITICAL
- GPU clock drops to THROTTLED or LOCKED while under load
- Memory usage > 85% with swap active
- GPU or CPU temperature exceeds 80°C

(sparkview also treats `PROCHOT` hardware throttle as a trigger — not available
to us since `spark_hwmon` is out of scope; see above.)

Tune from there once we have real multi-node history to look at.

## Collection architecture

See [architecture.md](architecture.md) for how these get scraped, stored, and
served — this file is deliberately just the "what," not the "how."
