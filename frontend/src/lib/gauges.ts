/** Gauges: System Activity's present tense (roadmap BA).
 *
 * The charts answer "how did it get here"; a gauge answers "is it fine" at a
 * glance. Same metrics, same node colours, read from the live snapshot that
 * already arrives every 1-2s, so a dial moves faster than Prometheus scrapes.
 *
 * Plain TypeScript with no runes and no DOM, so tests/js runs it for real.
 */

import type { MetricSpec } from './history';
import type { NodeSnapshot } from './types';
import { ENGINE_RUNTIMES } from './types';

/** How a metric is drawn as a gauge.
 *
 * `dial` needs a ceiling. A metric with none (throughput, prefill, swap I/O,
 * headroom) is a `tile`: a needle against an invented maximum would look
 * precise and say nothing. */
export type GaugeKind = 'dial' | 'tile';

/** Where the five metrics the snapshot does not carry come from: Prometheus's
 *  latest sample, through /api/latest. Everything else is read live. */
export const PROMETHEUS_ONLY = new Set([
  'cpu_clock',
  'swap_io',
  'psi_cpu_some',
  'psi_io_some',
  'disk_busy',
]);

const sum = (xs: (number | null | undefined)[]) => xs.reduce<number>((a, x) => a + (x ?? 0), 0);

function maxOf(xs: number[]): number | null {
  return xs.length ? Math.max(...xs) : null;
}

/** Decode tok/s on this node: the routers plus every engine, the same sum the
 *  node card shows. */
function decode(n: NodeSnapshot): number {
  const rt = n.runtimes;
  return (
    sum((rt?.llama_cpp ?? []).map((r) => r.generation_tokens_per_sec)) +
    sum(ENGINE_RUNTIMES.flatMap((e) => (rt?.[e] ?? []).map((v) => v.generation_tokens_per_sec)))
  );
}

/** Prefill tok/s: per model on the routers, per engine otherwise. */
function prefill(n: NodeSnapshot): number {
  const rt = n.runtimes;
  return (
    sum((rt?.llama_cpp ?? []).flatMap((r) => r.models.map((m) => m.prompt_tokens_per_sec))) +
    sum(ENGINE_RUNTIMES.flatMap((e) => (rt?.[e] ?? []).map((v) => v.prompt_tokens_per_sec)))
  );
}

function domainMax(n: NodeSnapshot, domain: string): number | null {
  return maxOf((n.temperatures ?? []).filter((t) => t.domain === domain).map((t) => t.celsius));
}

/** The live reading for one metric on one node, in the metric's units.
 *
 * Each one is the quantity the matching chart plots, taken from the snapshot
 * instead of from Prometheus, so a dial and the line beside it agree. Null
 * when the node does not report it — the dial then reads "—", never zero. */
export function liveValue(key: string, n: NodeSnapshot): number | null {
  if (!n.up) return null;
  switch (key) {
    case 'gpu_utilization':
      return n.gpu?.util_pct ?? null;
    case 'gpu_temperature':
      return n.gpu?.temp_c ?? null;
    case 'gpu_power':
      return n.gpu?.power_w ?? null;
    case 'gpu_clock':
      return n.gpu?.clock_mhz ?? null;
    case 'memory_used_percent':
      return n.memory && n.memory.total_bytes > 0
        ? (100 * n.memory.used_bytes) / n.memory.total_bytes
        : null;
    case 'cpu_utilization':
      return n.cpu?.util_pct ?? null;
    case 'psi_some_avg10':
      return n.psi?.some_avg10 ?? null;
    case 'psi_full_avg10':
      return n.psi?.full_avg10 ?? null;
    case 'tokens_per_second':
      return decode(n);
    case 'prompt_tokens_per_second':
      return prefill(n);
    // The GPU is in `temperatures` too, as the exporter's headline counts it.
    case 'system_temperature':
      return maxOf((n.temperatures ?? []).map((t) => t.celsius));
    case 'package_temperature':
      return domainMax(n, 'package');
    case 'storage_temperature':
      return domainMax(n, 'storage');
    case 'network_temperature':
      return domainMax(n, 'network');
    case 'temperature_headroom': {
      const left = (n.temperatures ?? [])
        .filter((t) => t.limit_c != null)
        .map((t) => (t.limit_c as number) - t.celsius);
      return left.length ? Math.min(...left) : null;
    }
    default:
      return null;
  }
}

/** The dial's full scale: what the charts' FULL mode uses, so a needle at
 *  half is the line at half the axis. Null means the metric is a tile. */
export function ceilingOf(m: MetricSpec): number | null {
  if (m.percent) return 100;
  return m.scaleMax ?? null;
}

export function kindOf(m: MetricSpec): GaugeKind {
  return ceilingOf(m) == null ? 'tile' : 'dial';
}

/** [warning, critical] for this node, where the hardware states them.
 *
 * Only the two temperatures the alerts and the node card judge: the GPU's,
 * from NVML's slowdown threshold, and the package zones', from the thermal
 * trip. Other sensors carry one limit each and no warning band, and a mark
 * invented for them would claim a threshold nothing enforces. */
export function bandsOf(key: string, n: NodeSnapshot): [number, number] | null {
  const b = n.temp_bands;
  if (!b) return null;
  if (key === 'gpu_temperature') return [b.gpu_warning_c, b.gpu_critical_c];
  if (key === 'package_temperature') return [b.cpu_warning_c, b.cpu_critical_c];
  return null;
}

export type GaugeState = 'ok' | 'warning' | 'critical';

/** STRICTLY GREATER THAN, as the node card and health.py judge it, so a dial
 *  never turns amber a degree before the health pill agrees. */
export function stateOf(v: number | null, bands: [number, number] | null): GaugeState {
  if (v == null || !bands) return 'ok';
  if (v > bands[1]) return 'critical';
  if (v > bands[0]) return 'warning';
  return 'ok';
}

/** Status is never colour alone: the word that goes with it. */
export const STATE_WORD: Record<GaugeState, string> = {
  ok: '',
  warning: '▲ warn',
  critical: '■ crit',
};

/* --- Geometry ------------------------------------------------------- */

/** The dial's sweep: 240 degrees, open at the bottom, starting lower left. */
export const START_DEG = 150;
export const SWEEP_DEG = 240;

/** An SVG arc along the dial from fraction `f0` to `f1` of the sweep, on a
 *  circle of radius `r` centred at (50, 50). Fractions are clamped, and an
 *  empty arc is a zero-length path rather than an invalid one. */
export function arcPath(f0: number, f1: number, r: number): string {
  const clamp = (f: number) => Math.min(1, Math.max(0, f));
  const a = clamp(f0);
  const b = Math.max(a, clamp(f1));
  const a0 = START_DEG + SWEEP_DEG * a;
  const a1 = START_DEG + SWEEP_DEG * b;
  const pt = (deg: number) => {
    const t = (deg * Math.PI) / 180;
    return `${(50 + r * Math.cos(t)).toFixed(2)} ${(50 + r * Math.sin(t)).toFixed(2)}`;
  };
  if (b - a < 1e-6) return `M${pt(a0)}`;
  return `M${pt(a0)} A${r} ${r} 0 ${a1 - a0 > 180 ? 1 : 0} 1 ${pt(a1)}`;
}

/** The dial's height over its width: the 240-degree arc leaves the bottom
 *  fifth of a square empty, so the drawing is cropped to 100 x 80. */
export const DIAL_ASPECT = 0.8;

/** Dial width for `n` dials in a `width` x `height` box, wrapping onto more
 *  lines when that makes each one larger. Each dial is `DIAL_ASPECT` as tall
 *  as it is wide, plus `label` px for the node's name under it; `gap` is the
 *  space between dials. Returns the width of one dial and how many go on a
 *  line. */
export function dialFit(
  n: number,
  width: number,
  height: number,
  gap: number,
  label = 0,
): { size: number; perLine: number } {
  if (n <= 0 || !(width > 0) || !(height > 0)) return { size: 0, perLine: Math.max(1, n) };
  let best = { size: 0, perLine: n };
  for (let lines = 1; lines <= n; lines++) {
    const perLine = Math.ceil(n / lines);
    const byWidth = (width - gap * (perLine - 1)) / perLine;
    const byHeight = (height - gap * (lines - 1) - label * lines) / lines / DIAL_ASPECT;
    const size = Math.floor(Math.min(byWidth, byHeight));
    if (size > best.size) best = { size, perLine };
  }
  return best;
}

/** A reading as the dial prints it: whole numbers for percentages, clocks
 *  and watts, one decimal for temperatures and token rates. */
export function formatReading(m: MetricSpec, v: number | null): string {
  if (v == null || !Number.isFinite(v)) return '—';
  if (m.unit === '°C' || m.unit === 'tok/s') return v.toFixed(1);
  return Math.round(v).toString();
}

/** "9s ago", for a reading that came from Prometheus rather than live. */
export function ageLabel(seconds: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return '';
  const s = Math.max(0, Math.round(seconds));
  return s < 120 ? `${s}s ago` : `${Math.round(s / 60)}m ago`;
}
