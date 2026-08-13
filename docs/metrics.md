# Metrics Catalog

What we actually want to scrape/collect, and from where. This is the source of truth
for exporter/scrape config once we start implementing.

## 1. GPU / system-level (per node)

**Source options:**

- [`dcgm-exporter`](https://github.com/NVIDIA/dcgm-exporter) — NVIDIA's standard
  Prometheus exporter, built on DCGM. Runs as a container
  (`nvcr.io/nvidia/k8s/dcgm-exporter`), needs `--gpus all --cap-add SYS_ADMIN`.
- [`dgx-spark-prometheus`](https://github.com/ateska/dgx-spark-prometheus) —
  purpose-built Prometheus exporter for DGX Spark / GB10 clusters specifically.
  Worth evaluating first since it's built for exactly this hardware, before
  reaching for the generic DCGM path.
- `nvidia-smi` (what we have today) — fine for ad hoc checks, not a metrics source
  for the dashboard (no persistence, no scraping-friendly format on its own).

**Known GB10 caveat — unified memory:** GB10 has no separate VRAM; CPU and GPU
share 128GB of LPDDR5x as one coherent pool. Standard NVML-based memory metrics
(what DCGM/`dcgm-exporter` normally reports) don't map cleanly onto this — expect
GPU memory-used numbers from vanilla `dcgm-exporter` to be unreliable or
misleading on GB10. `nvidia-smi --query-compute-apps` does report meaningful
per-process memory (GPU UUID, PID, process name, used memory) and is the fallback
for per-process attribution until/unless `dgx-spark-prometheus` or a DCGM update
handles this natively. Validate whatever exporter we pick against
`nvidia-smi --query-compute-apps` output before trusting its memory numbers.

**Known GB10 caveat — time-slicing:** if GPU time-slicing is ever used (e.g. under
k8s with `KUBERNETES_VIRTUAL_GPUS=true`), `dcgm-exporter` reports
utilization/power/temperature as identical across all virtual devices — i.e. it
can't currently distinguish per-slice load on Blackwell GB10. Not relevant to the
Docker Compose setup today, but worth remembering if the [roadmap](roadmap.md)
ever moves to k8s-based scheduling.

**Metrics to collect:**

- GPU utilization %
- Memory used / available (with the unified-memory caveat above — cross-check
  against `nvidia-smi --query-compute-apps`)
- Temperature
- Power draw
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

## Collection architecture

See [architecture.md](architecture.md) for how these get scraped, stored, and
served — this file is deliberately just the "what," not the "how."
