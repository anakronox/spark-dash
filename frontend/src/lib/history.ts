/** History queries, backed by Prometheus through the backend.
 *
 * Separate from the live feed on purpose. Prometheus is right about "what
 * happened"; only a direct poll is fresh enough for "what's happening". Mixing
 * them would make the live view as laggy as the scrape interval.
 */

export interface HistorySeries {
  node: string | null;
  labels: Record<string, string>;
  points: [number, number][];
}

export interface HistoryResponse {
  metric: string;
  series: HistorySeries[];
}

export interface MetricSpec {
  key: string;
  label: string;
  /** Axis suffix. Kept short — it repeats on every tick. */
  unit: string;
  /** Fixed 0-100 axis where the quantity is a percentage, so a quiet hour
   *  doesn't get auto-scaled into looking dramatic. */
  percent?: boolean;
}

export const METRICS: MetricSpec[] = [
  { key: 'gpu_utilization', label: 'GPU utilization', unit: '%', percent: true },
  { key: 'gpu_temperature', label: 'GPU temperature', unit: '°C' },
  { key: 'memory_used_percent', label: 'Memory used', unit: '%', percent: true },
  { key: 'tokens_per_second', label: 'Throughput', unit: 'tok/s' },
  { key: 'gpu_power', label: 'GPU power', unit: 'W' },
  { key: 'gpu_clock', label: 'GPU clock', unit: 'MHz' },
  { key: 'cpu_utilization', label: 'CPU utilization', unit: '%', percent: true },
  /* The 10-second average, deliberately: it shows a spike as a spike, which a
   * trend chart should. Note the pressure BAND on the node card follows the
   * 60-second average, so a brief peak here can sit above a calmer band — the
   * window is named in the label so that reads as intended rather than as a
   * contradiction. */
  { key: 'psi_some_avg10', label: 'Memory pressure (10s)', unit: '%', percent: true },
];

export interface RangeSpec {
  key: string;
  label: string;
  minutes: number;
  /** Chosen so each range yields 60–170 points: enough shape to read, few
   *  enough that Prometheus isn't asked to return thousands of samples
   *  nobody can see. */
  step: string;
}

export const RANGES: RangeSpec[] = [
  { key: '1h', label: '1h', minutes: 60, step: '60s' },
  { key: '6h', label: '6h', minutes: 360, step: '300s' },
  { key: '24h', label: '24h', minutes: 1440, step: '900s' },
  { key: '7d', label: '7d', minutes: 10080, step: '3600s' },
];

export async function fetchHistory(
  metric: string,
  minutes: number,
  step: string,
  signal?: AbortSignal,
): Promise<HistoryResponse> {
  const params = new URLSearchParams({ metric, minutes: String(minutes), step });
  const resp = await fetch(`/api/history?${params}`, { signal });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => null);
    throw new Error(detail?.detail ?? `history request failed (${resp.status})`);
  }
  return resp.json();
}

/** uPlot wants columnar data with one shared x axis.
 *
 * Series are aligned on the union of their timestamps rather than assumed
 * parallel: a node that was down for part of the window returns fewer points,
 * and zipping by index would silently shift its history sideways. Missing
 * samples become null, which uPlot draws as a gap — the honest rendering of
 * "we don't know", as opposed to a line interpolated straight through an
 * outage.
 */
export function toColumnar(
  series: HistorySeries[],
): { x: number[]; columns: number[][]; names: string[] } {
  const stamps = new Set<number>();
  for (const s of series) for (const [t] of s.points) stamps.add(t);
  const x = [...stamps].sort((a, b) => a - b);
  const index = new Map(x.map((t, i) => [t, i]));

  const columns: number[][] = [];
  const names: string[] = [];

  for (const s of series) {
    const col = new Array<number | null>(x.length).fill(null) as number[];
    for (const [t, v] of s.points) {
      const i = index.get(t);
      if (i !== undefined) col[i] = v;
    }
    columns.push(col);
    names.push(s.node ?? s.labels.instance ?? 'series');
  }

  return { x, columns, names };
}
