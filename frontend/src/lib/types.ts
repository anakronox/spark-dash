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
  router: string | null;
  /* Compute, as distinct from memory. A resident model holds tens of GiB while
   * using no SM at all, which looks identical to a busy one if you only read
   * bytes — this is the half that shows who is actually competing. */
  sm_pct: number;
  /* NVENC/NVDEC are SEPARATE fixed-function blocks: a transcoder at 70%
   * encoder is not competing for SM, so these stay apart from sm_pct. */
  encoder_pct: number;
  decoder_pct: number;
}

export interface NetworkInterface {
  name: string;
  up: boolean;
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
  kv_cache_pct: number | null;
  tokens_per_sec: number | null;
  requests_running: number;
  requests_waiting: number;
}

export interface LlamaRouterMetrics {
  endpoint: string;
  name: string;
  reachable: boolean;
  models: RouterModel[];
  max_instances: number | null;
  autoload: boolean | null;
  tokens_per_sec: number;
}

export interface VllmMetrics {
  model: string;
  requests_running: number;
  requests_waiting: number;
  kv_cache_pct: number | null;
  tokens_per_sec: number;
  prompt_tokens_total: number;
  generation_tokens_total: number;
}

export interface Runtimes {
  llama_cpp: LlamaRouterMetrics[];
  vllm: VllmMetrics[];
}

export interface NodeSnapshot {
  node_id: string;
  ts: string;
  up: boolean;
  /** Commit the agent image was built from. */
  agent_version: string;
  /** Cluster this node belongs to, or null when it stands alone.
   *  Grouped nodes pool memory for distributed inference, so their capacity
   *  sums; ungrouped ones don't, so it must not. */
  group: string | null;
  health: HealthState;
  health_reasons: string[];
  gpu: GpuMetrics | null;
  memory: MemoryMetrics | null;
  psi: PsiMetrics | null;
  cpu: CpuMetrics | null;
  processes: ProcessInfo[];
  network: NetworkInterface[];
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
export const LLM_RUNTIMES = new Set(['vllm', 'llama.cpp', 'sglang', 'tgi', 'ollama']);
