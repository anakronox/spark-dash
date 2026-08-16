/** History queries, backed by Prometheus through the backend.
 *
 * Separate from the live feed on purpose. Prometheus is right about "what
 * happened"; only a direct poll is fresh enough for "what's happening". Mixing
 * them would make the live view as laggy as the scrape interval.
 */

import { fetchWithTimeout } from './request';

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
  /** Categorical slot (1-8), fixed per metric. Colour follows the metric and
   *  not its position in the selection, so adding or removing one never
   *  repaints the others. */
  slot: number;
  /** Value that reads as 100% when this metric shares the common axis.
   *
   * FIXED, not the maximum observed in the window. An observed max would
   * rescale the line every time the range changed and would inflate a flat
   * quiet hour into something dramatic — the reading would depend on the
   * window rather than on the hardware. Omitted where no natural ceiling
   * exists (throughput), which falls back to the window's own maximum; that
   * series is then explicitly relative and the tooltip's absolute value is the
   * one to trust. */
  scaleMax?: number;
}

export const METRICS: MetricSpec[] = [
  { key: 'gpu_utilization', label: 'GPU utilization', unit: '%', percent: true, slot: 1 },
  // 100°C: above the 90°C at which this part shuts down, so the plotted
  // height stays meaningful right through the danger band.
  { key: 'gpu_temperature', label: 'GPU temperature', unit: '°C', slot: 2, scaleMax: 100 },
  { key: 'memory_used_percent', label: 'Memory used', unit: '%', percent: true, slot: 3 },
  { key: 'tokens_per_second', label: 'Throughput', unit: 'tok/s', slot: 4 },
  // The GB10 idles ~12W and peaks well under 300W.
  { key: 'gpu_power', label: 'GPU power', unit: 'W', slot: 5, scaleMax: 300 },
  // max_sm_clock as NVML reports it. Note the part actually runs ~2411MHz,
  // so a healthy clock sits around 80% here rather than at the top.
  { key: 'gpu_clock', label: 'GPU clock', unit: 'MHz', slot: 6, scaleMax: 3003 },
  { key: 'cpu_utilization', label: 'CPU utilization', unit: '%', percent: true, slot: 7 },
  /* The 10-second average, deliberately: it shows a spike as a spike, which a
   * trend chart should. Note the pressure BAND on the node card follows the
   * 60-second average, so a brief peak here can sit above a calmer band — the
   * window is named in the label so that reads as intended rather than as a
   * contradiction. */
  { key: 'psi_some_avg10', label: 'Memory pressure (10s)', unit: '%', percent: true, slot: 8 },
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
  const resp = await fetchWithTimeout(`/api/history?${params}`, { signal });
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

/** One line on the shared chart. */
export interface CombinedSeries {
  /** Legend text: the metric, plus the node when there's more than one. */
  label: string;
  metricKey: string;
  slot: number;
  unit: string;
  /** Plotted values, 0-100. */
  scaled: (number | null)[];
  /** The real readings, same indices — what the tooltip shows. */
  raw: (number | null)[];
  /** What 100% meant for this line.
   *  - a number: fixed ceiling, shown in the legend
   *  - null:     scaled to the window's own maximum; the UI flags it
   *  - undefined: already a percentage, so there's nothing to explain */
  scaleMax?: number | null;
}

export interface Combined {
  x: number[];
  series: CombinedSeries[];
}

/** Merge several metrics onto ONE time axis, normalised to a common 0-100.
 *
 * Two problems solved together.
 *
 * TIMESTAMPS. Metrics are queried with the same range and step, so Prometheus
 * usually returns aligned points — but "usually" isn't good enough: a metric
 * with no data early in the window comes back shorter, and zipping by index
 * would then plot every later value against the wrong instant. The union of
 * all timestamps is taken and missing points are left null, which uPlot draws
 * as a gap rather than interpolating through.
 *
 * UNITS. These metrics span %, °C, W, MHz and tok/s. Rendering them against one
 * raw axis is impossible (a 2400MHz clock would flatten everything else into
 * the baseline) and a second y-axis is worse — two scales let a chart imply a
 * correlation by arranging where the lines cross. Normalising to a percentage
 * of a FIXED ceiling keeps one honest axis, and the absolute reading stays
 * available in the tooltip, which is the number you actually act on.
 */
export function combine(
  parts: { metric: MetricSpec; data: { x: number[]; columns: number[][]; names: string[] } }[],
  { labelNodes, stepSeconds }: { labelNodes: boolean; stepSeconds: number },
): Combined {
  /* TIMESTAMPS ARE SNAPPED TO THE STEP GRID BEFORE MERGING.
   *
   * Each metric is a separate request and the backend computes its own
   * `end = time.time()`, so parallel requests come back on grids offset by
   * milliseconds — 1786875344.312 against 1786875344.313. Taking a raw union
   * of those produced one timestamp per metric per instant, leaving every
   * series null at all the others' points. With `points: {show: false}` that
   * renders as a completely empty plot: the axes and gridlines drawn, no
   * lines at all. Snapping collapses them back onto one grid. */
  const snap = (ts: number) => Math.round(ts / stepSeconds) * stepSeconds;

  const stamps = new Set<number>();
  for (const p of parts) for (const ts of p.data.x) stamps.add(snap(ts));
  const x = [...stamps].sort((a, b) => a - b);
  const index = new Map(x.map((ts, i) => [ts, i]));

  const series: CombinedSeries[] = [];
  for (const { metric, data } of parts) {
    data.columns.forEach((col, ci) => {
      const raw: (number | null)[] = new Array(x.length).fill(null);
      for (let i = 0; i < data.x.length; i++) {
        const at = index.get(snap(data.x[i]));
        if (at !== undefined) raw[at] = col[i] ?? null;
      }

      // Percentages are already on the axis; everything else is divided by its
      // ceiling. Falling back to the observed max keeps a series with no
      // natural ceiling (throughput) legible rather than invisible.
      let ceiling = metric.percent ? 100 : (metric.scaleMax ?? 0);
      let fixed = metric.percent || metric.scaleMax !== undefined;
      if (!ceiling) {
        const observed = Math.max(...raw.filter((v): v is number => v != null), 0);
        ceiling = observed > 0 ? observed : 1;
        fixed = false;
      }

      series.push({
        label: labelNodes && data.names[ci] ? `${metric.label} · ${data.names[ci]}` : metric.label,
        metricKey: metric.key,
        slot: metric.slot,
        unit: metric.unit,
        raw,
        scaled: raw.map((v) => (v == null ? null : (v / ceiling) * 100)),
        // A percentage metric IS the axis, so it carries no ceiling caption:
        // "100% = 100%" is noise. Non-percent metrics report theirs; a series
        // with no fixed ceiling reports null and is flagged as relative.
        scaleMax: metric.percent ? undefined : fixed ? ceiling : null,
      });
    });
  }

  return { x, series };
}
