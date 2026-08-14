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
  model: string | null;
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
  health: HealthState;
  health_reasons: string[];
  gpu: GpuMetrics | null;
  memory: MemoryMetrics | null;
  psi: PsiMetrics | null;
  cpu: CpuMetrics | null;
  processes: ProcessInfo[];
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
