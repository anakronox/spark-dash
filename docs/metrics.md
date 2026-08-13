# Metrics Catalog

What we actually want to scrape/collect, and from where. This is the source of truth
for exporter/scrape config once we start implementing.

## 1. GPU / system-level (per node)

Split into two sources rather than one: a **baseline exporter** for standard
multi-GPU/Prometheus plumbing, plus a **custom `gb10-node-exporter`** (ours to
build) for the GB10-specific signals that nothing generic exposes correctly.
This split exists because of hard lessons already documented by
[`sparkview`](https://github.com/parallelArchitect/sparkview) — a GB10-aware TUI
monitor (see [related tools](https://github.com/parallelArchitect) — same author
also publishes `nvml-unified-shim`, `spark-gpu-throttle-check`, and
`cuda-unified-memory-analyzer`) — and worth treating as the reference
implementation rather than re-deriving these from scratch.

### Baseline exporter

- [`dcgm-exporter`](https://github.com/NVIDIA/dcgm-exporter) — NVIDIA's standard
  Prometheus exporter, built on DCGM. Runs as a container
  (`nvcr.io/nvidia/k8s/dcgm-exporter`), needs `--gpus all --cap-add SYS_ADMIN`.
  Optionally paired with
  [`nvml-unified-shim`](https://github.com/parallelArchitect/nvml-unified-shim)
  to correct its memory reporting on UMA platforms (see caveat below).
- [`dgx-spark-prometheus`](https://github.com/ateska/dgx-spark-prometheus) —
  purpose-built Prometheus exporter for DGX Spark / GB10 clusters specifically.
  Still worth evaluating as an alternative to `dcgm-exporter` + shim.
- `nvidia-smi` (what we have today) — fine for ad hoc checks, not a metrics
  source for the dashboard on its own.

### `gb10-node-exporter` (custom, ours to build)

A small exporter, modeled directly on `sparkview`'s validated techniques, that
covers what `dcgm-exporter`/`dgx-spark-prometheus` don't:

- **Unified memory, done correctly:** `nvmlDeviceGetMemoryInfo` on GB10 reports
  `total ≈ MemTotal` regardless of actual usage — not useful. Use
  `vm.total - vm.available` (`/proc/meminfo` `MemTotal`/`MemAvailable`, or
  `psutil.virtual_memory()`) for used memory instead, with `vm.total` as the
  display total. This is accurate under heavy inference load; `nvml-unified-shim`
  does the same fix at the NVML layer if we'd rather patch the baseline exporter
  than duplicate the logic.
- **Memory pressure (PSI):** `/proc/pressure/memory` gives a LOW/MOD/HIGH/CRITICAL
  signal that catches contention *before* swap or a system freeze — raw
  percent-used doesn't. Worth its own gauge/alert, not just folded into the
  memory-used number.
- **Clock throttle state:** a load-gated state machine —
  `IDLE` (not under load, not evaluated) / `PASS` (healthy under load) /
  `LOCKED` (externally capped via `nvidia-smi -lgc`) / `THROTTLED` (low clock
  under load — power-delivery issue suspected). sparkview's field-derived
  threshold: healthy sustained load runs ~2400MHz; **clock < 1400MHz under
  sustained load → THROTTLED**, degraded systems have been observed in the
  500-850MHz range. This is a real "something's wrong with this node" signal
  that neither NVML nor DCGM surfaces on its own.
- **GB10 power rails:** requires the
  [`spark_hwmon`](https://github.com/antheas/spark_hwmon) kernel module
  (installed via `dkms` — a real per-node system dependency, not just app
  config; see [roadmap.md](roadmap.md)). Once installed, exposes `gpu`,
  `dc_input`, `syspl1`, `PROCHOT`, power-limit level, and `Tj-rise` — actual
  hardware power telemetry rather than an estimate. `PROCHOT` active is itself
  an important alert condition.
- **Per-process GPU memory attribution** — `nvidia-smi --query-compute-apps`
  (GPU UUID, PID, process name, used memory), for tying usage back to a
  specific llama.cpp/vLLM container and for the process-list panel (see
  [architecture.md](architecture.md#live-view-fast-path)).

**Known GB10 caveat — time-slicing:** if GPU time-slicing is ever used (e.g.
under k8s with `KUBERNETES_VIRTUAL_GPUS=true`), `dcgm-exporter` reports
utilization/power/temperature as identical across all virtual devices — i.e. it
can't currently distinguish per-slice load on Blackwell GB10. Not relevant to
the Docker Compose setup today, but worth remembering if the
[roadmap](roadmap.md) ever moves to k8s-based scheduling.

**Metrics to collect:**

- GPU utilization %
- Memory used / available — via `gb10-node-exporter`'s UMA-correct calc, not raw
  NVML
- Memory pressure (PSI) state
- Clock state (IDLE/PASS/LOCKED/THROTTLED)
- Temperature (GPU + CPU, current and session peak)
- Power draw, plus GB10 power-rail detail where `spark_hwmon` is installed
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

## 5. Anomaly thresholds (starting point for Phase 3 alerting)

`sparkview`'s anomaly auto-logger already field-validated a set of trigger
conditions on GB10 hardware — reuse these as the initial Prometheus alerting
rules ([roadmap.md](roadmap.md) Phase 3) instead of guessing at thresholds from
scratch:

- Memory pressure (PSI) reaches MOD, HIGH, or CRITICAL
- GPU clock drops to THROTTLED or LOCKED while under load
- Memory usage > 85% with swap active
- GPU or CPU temperature exceeds 80°C
- `PROCHOT` hardware throttle active (GB10 `spark_hwmon` only)

Tune from there once we have real multi-node history to look at.

## Collection architecture

See [architecture.md](architecture.md) for how these get scraped, stored, and
served — this file is deliberately just the "what," not the "how."
