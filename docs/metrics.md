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
- `vllm:time_to_first_token_seconds` (histogram)
- `vllm:e2e_request_latency_seconds` (histogram)
- `vllm:request_success_total` / failure counters
- GPU cache block eviction / prefix-cache hit rate, if we want cache-efficiency
  panels later

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

## 4. Derived / cluster-level

Computed by the dashboard backend, not scraped directly:

- Cluster-wide GPU utilization / free capacity across all 3 nodes
- "What's running where" table: node × runtime × model × status
- Aggregate tokens/sec across the whole cluster
- Node health summary (up/down, last-seen)

### The `group` label, and why totals are usually wrong

Every scraped series carries `node`, and a **clustered** node also carries
`group`. The label is attached at scrape time from the `file_sd` target files
the backend renders out of `SPARK_NODES` — the agent can't supply it, because a
node has no way to know what it has been clustered with.

This exists because **memory sums within a group and never across groups.**
Clustered nodes pool memory for distributed inference, so a model can span them
and their combined free space is real. Unclustered nodes can't do that, so
adding their free memory together describes capacity that does not exist.

```promql
# Free memory per group — the honest unit of capacity.
sum by (group) (
  sparkdash_memory_available_bytes
    * on(node) group_left(group) (max by (node, group) (up{job="spark-dash-agent"}))
)

# The question that actually matters: what is the largest model that fits?
# The best single group can offer — NOT the sum of all groups.
max(
  sum by (group) (sparkdash_memory_available_bytes)
)
```

Two traps:

- **A standalone node carries no `group` label at all**, deliberately: an empty
  label would create a distinct series and a phantom group in aggregation. In
  PromQL that means `sum by (group)` buckets every standalone node together
  under the empty string. Where that matters, group on `node` for the standalone
  case, which is what the backend's `group_key` does — a standalone node is a
  group of one.
- **`sum` without `by (group)` is almost always the wrong answer.** It reads as
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
