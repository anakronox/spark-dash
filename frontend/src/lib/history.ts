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
  { key: 'gpu_utilization', label: 'GPU utilization', unit: '%', percent: true },
  // 100°C: above the 90°C at which this part shuts down, so the plotted
  // height stays meaningful right through the danger band.
  { key: 'gpu_temperature', label: 'GPU temperature', unit: '°C', scaleMax: 100 },
  { key: 'memory_used_percent', label: 'Memory used', unit: '%', percent: true },
  { key: 'tokens_per_second', label: 'Throughput', unit: 'tok/s' },
  /* Prefill as its own chip. It was previously added INTO Throughput, which
     made that chart spike three orders of magnitude while a prompt was being
     ingested — the chart said 47,672 while the model generated 48. */
  { key: 'prompt_tokens_per_second', label: 'Prefill', unit: 'tok/s' },
  // The GB10 idles ~12W and peaks well under 300W.
  { key: 'gpu_power', label: 'GPU power', unit: 'W', scaleMax: 300 },
  // max_sm_clock as NVML reports it. Note the part actually runs ~2411MHz,
  // so a healthy clock sits around 80% here rather than at the top.
  { key: 'gpu_clock', label: 'GPU clock', unit: 'MHz', scaleMax: 3003 },
  { key: 'cpu_utilization', label: 'CPU utilization', unit: '%', percent: true },
  /* Grace cores throttling would slow prompt processing invisibly — the
     CPU-side equivalent of the GPU clock check. Averaged across cores rather
     than maxed, because throttling shows up as every core dropping together
     and a max would be held up by whichever one happened to boost.
     3500MHz: the part is observed at 3354MHz, so a healthy clock sits near the
     top of this axis rather than at it. */
  { key: 'cpu_clock', label: 'CPU clock', unit: 'MHz', scaleMax: 3500 },
  /* The 10-second average, deliberately: it shows a spike as a spike, which a
   * trend chart should. Note the pressure BAND on the node card follows the
   * 60-second average, so a brief peak here can sit above a calmer band — the
   * window is named in the label so that reads as intended rather than as a
   * contradiction. */
  { key: 'psi_some_avg10', label: 'Memory pressure (10s)', unit: '%', percent: true },
  /* SOME vs FULL, and the distinction is the whole point. "Some" means at least
     one task stalled waiting on memory, which a busy inference box does
     routinely and which the chart above will show as a lively line. "Full"
     means EVERY runnable task was stalled — nothing progressed at all.

     On a node whose entire job is holding models in one shared pool, that is
     the difference between working hard and stopped, and it is the strongest
     distress signal the agent produces. It should normally be a flat zero;
     treat any sustained lift as the node having stopped rather than slowed.

     Same 10s window as its sibling deliberately, so the two can be read
     against each other. */
  { key: 'psi_full_avg10', label: 'Memory stalled (10s)', unit: '%', percent: true },
  /* "SLOW" HAS AT LEAST THREE CAUSES and until now the dashboard could
     distinguish one. Memory pressure said the box was thrashing; a machine
     stalled on CPU runqueue or on disk read looked identical to a healthy one.
     These are the other two.
     Note the smoothing differs from the memory gauge above it. That one is the
     kernel's own 10-second average, read by the agent. These are counters of
     seconds stalled, so the backend takes a rate over a window scaled to the
     chart's step — same units and the same meaning, but a longer and
     step-dependent smoothing. A spike an hour ago will read lower here than
     the memory chart would have shown it. */
  /* Swap TRAFFIC, not swap occupancy. A node can hold gigabytes of cold pages
     swapped out and be perfectly healthy; thrashing is a rate, and reading the
     resident figure as the symptom would prompt exactly the wrong reaction on a
     unified-memory box where some swap is normal.

     This plots the quantity `SwapThrashing` alerts on — rate(pswpin) +
     rate(pswpout) > 50 for 10m — so the chart and the alert cannot disagree
     about what thrashing is.

     No scaleMax: there is no natural ceiling, and a fixed one would clip the
     real event this exists to show. The series is relative and the tooltip's
     absolute value is the one to read. */
  { key: 'swap_io', label: 'Swap I/O', unit: 'pages/s' },
  { key: 'psi_cpu_some', label: 'CPU pressure', unit: '%', percent: true },
  { key: 'psi_io_some', label: 'I/O pressure', unit: '%', percent: true },
  /* Explains a slow cold start: weights coming off disk. Maxed across devices,
     not averaged — saturation is "is any disk pegged", and averaging a busy
     disk against an idle one reports a comfortable 50% for a machine that is
     completely stalled on one of them. */
  { key: 'disk_busy', label: 'Disk busy', unit: '%', percent: true },
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

/** Snap a series' timestamps onto the query's step grid.
 *
 * WHY THIS SURVIVED THE SPLIT INTO SMALL MULTIPLES. It began as the fix for a
 * merge: several metrics were combined onto one x axis, and because each is a
 * separate range query whose backend computes its own `end = time.time()`, the
 * grids came back offset by milliseconds — 1786875344.312 against .313. A raw
 * union produced one timestamp per metric per instant, leaving every series
 * null at all the others' points, which rendered as a completely empty plot.
 *
 * There is no merge any more, so that failure is gone. The snapping is not:
 * the charts are now read as a GRID, and x domains differing by a fraction of a
 * step put each plot's gridlines — and the synchronised crosshair — at slightly
 * different pixels. Snapping is what makes eight small multiples line up column
 * for column.
 *
 * Collisions are folded rather than duplicated: two raw stamps landing on one
 * grid point keep the later sample, which is the fresher reading.
 */
export function snapGrid(
  x: number[],
  columns: number[][],
  stepSeconds: number,
): { x: number[]; columns: number[][] } {
  if (!stepSeconds || !x.length) return { x, columns };
  const snap = (ts: number) => Math.round(ts / stepSeconds) * stepSeconds;

  const stamps: number[] = [];
  const at = new Map<number, number>();
  for (const ts of x) {
    const s = snap(ts);
    if (!at.has(s)) {
      at.set(s, stamps.length);
      stamps.push(s);
    }
  }
  if (stamps.length === x.length && stamps.every((s, i) => s === x[i])) {
    return { x, columns };
  }

  const out = columns.map(() => new Array<number | null>(stamps.length).fill(null));
  for (let i = 0; i < x.length; i++) {
    const j = at.get(snap(x[i]))!;
    for (let c = 0; c < columns.length; c++) {
      const v = columns[c][i];
      if (v != null) out[c][j] = v;
    }
  }
  return { x: stamps, columns: out as number[][] };
}

/** One event to mark on the charts. */
export interface Annotation {
  ts: number;
  /** "alert" | "cold-start" | "deploy" — drives colour and wording. */
  kind: string;
  label: string;
  node: string | null;
}

/** Events worth drawing on the history charts.
 *
 * ONE request, not three. The backend decides what deserves to be a mark —
 * alerts that fired, cold starts, deploys — because that filtering is the
 * feature and belongs in one place with its reasoning. Drawing everything was
 * measured at 173 events in a 7-day window on a 390px chart, which is a grey
 * wash rather than an annotation layer.
 */
export async function fetchAnnotations(
  minutes: number,
  step: string,
  signal?: AbortSignal,
): Promise<Annotation[]> {
  const resp = await fetchWithTimeout(
    `/api/annotations?minutes=${minutes}&step=${encodeURIComponent(step)}`,
    { signal },
  );
  if (!resp.ok) throw new Error(String(resp.status));
  return (await resp.json()).annotations ?? [];
}
