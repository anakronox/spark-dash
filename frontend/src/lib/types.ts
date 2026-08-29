/** Mirrors the backend's snapshot contract (common/spark_dash_common/models.py).
 *
 * Hand-written rather than generated: the surface is small, and keeping it
 * explicit makes it obvious when the Python side changes shape.
 */

export type ClockState = 'IDLE' | 'PASS' | 'LOCKED' | 'THROTTLED';
export type PsiState = 'LOW' | 'MOD' | 'HIGH' | 'CRITICAL';
export type HealthState = 'good' | 'warning' | 'serious' | 'critical';
export type ModelState = 'active' | 'sleeping' | 'loading' | 'unloaded' | 'unknown';

export interface GpuMetrics {
  util_pct: number;
  temp_c: number | null;
  power_w: number | null;
  clock_mhz: number | null;
  clock_state: ClockState;
}

export interface MemoryMetrics {
  total_bytes: number;
  available_bytes: number;
  used_bytes: number;
  swap_used_bytes: number;
  /** True on GB10: CPU and GPU share one coherent pool, so this IS GPU memory. */
  unified: boolean;
}

export interface DiskMetrics {
  total_bytes: number;
  available_bytes: number;
  /** total - available, matching what the disk alerts measure. */
  used_bytes: number;
}

export interface TempBands {
  gpu_warning_c: number;
  gpu_critical_c: number;
  /** "hardware" when read off the device, otherwise a fallback estimate.
   *  A threshold you cannot trust must not be presented like one you can. */
  gpu_source: string;
  cpu_warning_c: number;
  cpu_critical_c: number;
  cpu_source: string;
}

export interface PsiMetrics {
  some_avg10: number;
  some_avg60: number;
  full_avg10: number;
  full_avg60: number;
  state: PsiState;
}

export interface CpuMetrics {
  util_pct: number;
  temp_c: number | null;
  load_avg_1m: number | null;
  active_cores: number | null;
}

export interface ProcessInfo {
  pid: number;
  name: string;
  gpu_mem_bytes: number;
  runtime: string | null;
  /* From `--alias` in the process's argv — the same name the router reports,
   * so a row here lines up with a row in the models table. Null for a router
   * parent (which serves every model and holds none) and for non-LLM work. */
  model: string | null;
  /* True when this process holds part of a model served by the CLUSTER rather
   * than by this node — a tensor-parallel worker with no endpoint of its own,
   * whose model name the backend filled in from the head node. Flagged so an
   * inferred name is distinguishable from one the process reported itself. */
  shard: boolean;
  /* host:port this model is served from — a router for llama.cpp, the
   * instance's own endpoint for vLLM. See ProcessInfo.server in the agent. */
  server: string | null;
  /* Compute, as distinct from memory. A resident model holds tens of GiB while
   * using no SM at all, which looks identical to a busy one if you only read
   * bytes — this is the half that shows who is actually competing. */
  sm_pct: number;
  /* NVENC/NVDEC are SEPARATE fixed-function blocks: a transcoder at 70%
   * encoder is not competing for SM, so these stay apart from sm_pct. */
  encoder_pct: number;
  decoder_pct: number;
}

export interface TempSensor {
  /** `package`, `storage`, `network`, `wireless`, `gpu`, or `other` for a chip
   *  the agent's classifier doesn't recognise — never dropped. */
  domain: string;
  sensor: string;
  celsius: number;
  /** The limit THIS sensor reports. Differs by twenty degrees across one box —
   *  104.8 package trip, 84.85 nvme, 105 NIC asic, GPU shutdown — so a single
   *  global threshold would be wrong for four of the five domains.
   *  Null where the hardware states none, which is every wifi phy. Absent is
   *  not unlimited, and a row with no limit gets no headroom rather than an
   *  invented one. */
  limit_c: number | null;
}

export interface NetworkInterface {
  name: string;
  up: boolean;
  /** False when this node's config excludes the interface from alerting.
   *  Excluded by name, never selected by name — an interface nobody has
   *  configured is still watched, so forgetting the list is noisy rather than
   *  silent. Reported either way: the panel keeps showing it. */
  monitored: boolean;
  /** Three FACTS about what kind of NIC this is, from sysfs, so the card can
   *  divide interfaces into RoCE, Management, WiFi and Other. Optional because
   *  an older agent does not send them, and absent must read as "not known"
   *  rather than as any group. The rule lives in network-history.ts. */
  wireless?: boolean;
  driver?: string | null;
  bus?: string | null;
  /** Negotiated link speed. Null when the driver doesn't report one. */
  speed_mbps: number | null;
  rx_bytes_per_sec: number;
  tx_bytes_per_sec: number;
  rx_bytes_total: number;
  tx_bytes_total: number;
  rx_errors: number;
  tx_errors: number;
  rx_dropped: number;
  tx_dropped: number;
}

export interface RdmaPort {
  device: string;
  port: number;
  /** Derived from the Ethernet interface this port is paired with — one cable
   *  carries both, so they are excluded together or not at all. */
  monitored: boolean;
  state: string;
  physical_state: string;
  /** "Ethernet" for RoCE, "InfiniBand" for native IB. The GX10s run RoCEv2. */
  link_layer: string;
  /** Verbatim from the driver, e.g. "200 Gb/sec (2X NDR)". A link that
   *  negotiated below its rated speed is otherwise invisible. */
  rate: string;
  /** Ethernet interface this RoCE device is paired with; byte counters come
   *  from there, since mlx5 leaves the IB-style counters at zero. */
  interface: string;
  rx_bytes_per_sec: number;
  tx_bytes_per_sec: number;
  rx_bytes_total: number;
  tx_bytes_total: number;
  errors: number;
}

export interface RouterModel {
  name: string;
  state: ModelState;
  raw_status: string;
  slots_used: number;
  slots_total: number;
  /** Null for an engine that does not report OCCUPANCY. SGLang publishes
   *  `cache_hit_rate` — the fraction of prompt tokens served from the prefix
   *  cache — which has the same shape and answers a different question, so its
   *  rows leave this empty rather than showing a number that reads as how full
   *  the cache is. */
  kv_cache_pct: number | null;
  /** Prefill and decode combined. Kept for continuity; read the generation
   *  rate instead — see EngineMetrics. */
  tokens_per_sec: number | null;
  /** Decode: tokens generated per second. THE throughput number. */
  generation_tokens_per_sec: number | null;
  /** Prefill: prompt tokens ingested per second. */
  prompt_tokens_per_sec: number | null;
  requests_running: number;
  requests_waiting: number;
  /** From llama.cpp's `meta`. Null on vLLM and on older builds — null means
   *  unknown, never zero. */
  size_bytes: number | null;
  n_params: number | null;
  quantization: string | null;
  context_length: number | null;
}

export interface LlamaRouterMetrics {
  endpoint: string;
  name: string;
  reachable: boolean;
  models: RouterModel[];
  max_instances: number | null;
  autoload: boolean | null;
  /** Router roll-up, prefill and decode combined. */
  tokens_per_sec: number;
  /** Router roll-up of decode only — what the cards and the header sum. */
  generation_tokens_per_sec: number;
}

/** One vLLM or SGLang instance. They answer the same questions in the same
 *  shape — only the metric NAMES differ, and those are the agent's problem —
 *  so one type covers both rather than two identical ones drifting apart. */
export interface EngineMetrics {
  model: string;
  /** host:port. Nothing fronts these engines, so this is its own endpoint. */
  /** False when the configured endpoint did not answer. Reported rather
   *  than omitted: an instance dropped from the list is indistinguishable
   *  from a node that runs none. */
  reachable: boolean;
  server: string;
  requests_running: number;
  requests_waiting: number;
  kv_cache_pct: number | null;
  /** Prefill and decode added together. Kept because recorded history and the
   *  Grafana dashboard are written against it, but it is not the number to
   *  show: measured on a live cluster this read 47,672 tok/s while the model
   *  was generating 48. A big prompt inside one poll window is a real ingest
   *  rate and is not what "throughput" means. */
  tokens_per_sec: number;
  /** Decode: tokens generated per second. THE throughput number. */
  generation_tokens_per_sec: number;
  /** Prefill: prompt tokens ingested per second. */
  prompt_tokens_per_sec: number;
  prompt_tokens_total: number;
  generation_tokens_total: number;
}

export interface Runtimes {
  llama_cpp: LlamaRouterMetrics[];
  vllm: EngineMetrics[];
  sglang: EngineMetrics[];
}

/** The engine fields of `Runtimes`, keyed by runtime name — the frontend's
 *  copy of ENGINE_RUNTIMES in the Python models. Everything that walks "every
 *  engine on this node" goes through here, so adding one is a key rather than
 *  another `.vllm` beside every existing one.
 *
 *  Tolerates a snapshot from an older backend that has no `sglang` key: the
 *  dashboard is deployed separately from the agents it reads. */
export const ENGINE_RUNTIMES = ['vllm', 'sglang'] as const;

export function engines(runtimes: Runtimes): [string, EngineMetrics[]][] {
  return ENGINE_RUNTIMES.map((r) => [r, runtimes?.[r] ?? []]);
}

/** Whether a Prometheus job is an engine — the job name IS the runtime name,
 *  one job per engine. Gates the retire button: an engine endpoint is
 *  configuration and can be removed, while an infrastructure target describes
 *  hardware that still exists, and removing it would blind the dashboard to a
 *  real failure. */
export function isEngineJob(job: string): boolean {
  return (ENGINE_RUNTIMES as readonly string[]).includes(job);
}

export interface NodeSnapshot {
  node_id: string;
  ts: string;
  up: boolean;
  /** Commit the agent image was built from. */
  agent_version: string;
  /** Cluster this node belongs to, or null when it stands alone.
   *  Clustered nodes pool memory for distributed inference, so their capacity
   *  sums; unclustered ones don't, so it must not.
   *
   *  A NAME, never a count — "pair" stops being true at three nodes, and
   *  clusters in the wild run to 32. */
  cluster: string | null;
  health: HealthState;
  health_reasons: string[];
  /** Runtimes running on this node with nothing configured to collect them.
   *  A gap here is a silence — the node looks healthy because everything
   *  being measured is healthy. */
  unmonitored_runtimes: string[];
  gpu: GpuMetrics | null;
  memory: MemoryMetrics | null;
  disk: DiskMetrics | null;
  temp_bands: TempBands | null;
  psi: PsiMetrics | null;
  cpu: CpuMetrics | null;
  processes: ProcessInfo[];
  network: NetworkInterface[];
  temperatures: TempSensor[];
  rdma: RdmaPort[];
  runtimes: Runtimes;
  errors: Record<string, string>;
}

export interface ClusterSnapshot {
  ts: string;
  nodes: NodeSnapshot[];
}

/** Runtimes that serve LLM inference, as opposed to other GPU consumers.
 *  Mirrors LLM_RUNTIMES in the agent — on GB10 both compete for one pool, so
 *  telling them apart is the difference between "12GB used" and "12GB used by
 *  ComfyUI". */
export const LLM_RUNTIMES = new Set(['vllm', 'llama.cpp', 'sglang', 'atlas', 'tgi', 'ollama']);
