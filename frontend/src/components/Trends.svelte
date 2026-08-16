<script lang="ts">
  /* History — the thing a TUI can't give you.
   *
   * The live view answers "what's happening"; this answers "how did it get
   * here". Both are needed: a GPU at 84°C means something different when it's
   * been climbing for an hour than when it just spiked.
   */
  import CombinedChart from './CombinedChart.svelte';
  import { METRICS, RANGES, combine, fetchHistory, toColumnar } from '../lib/history';
  import { metricColor, nodeColor } from '../lib/theme';

  interface Props {
    /** Ordered node ids, so chart colours match the cards. */
    nodeIds: string[];
    /** Changes whenever the theme does, so charts rebuild with the new
     *  canvas colours. */
    themeKey: string;
  }
  const { nodeIds, themeKey }: Props = $props();

  /* SELECTED METRICS ON ONE PLOT, against a fixed 0-100% axis.
   *
   * These span %, °C, W, MHz and tok/s, so they can't share a raw axis — a
   * 2400MHz clock would flatten everything else onto the baseline. Each series
   * is normalised against a FIXED ceiling (100°C, 300W, 3003MHz) rather than
   * given a second y-axis, because two scales let a chart imply a correlation
   * purely by where the scaling puts the crossings.
   *
   * The absolute reading is not lost: the tooltip reports the real value in
   * its own unit. Shape is comparable at a glance; the number you'd act on is
   * one hover away.
   */
  const STORAGE_KEY = 'spark-dash.trend-metrics.v1';

  function readSelection(): string[] {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? 'null');
      const known = new Set(METRICS.map((m) => m.key));
      const kept = Array.isArray(saved) ? saved.filter((k) => known.has(k)) : [];
      return kept.length ? kept : [METRICS[0].key];
    } catch {
      return [METRICS[0].key];
    }
  }

  let selected = $state<string[]>(readSelection());
  let rangeKey = $state(RANGES[0].key);
  let error = $state<string | null>(null);
  let loading = $state(false);

  interface Dataset {
    x: number[];
    columns: number[][];
    names: string[];
  }
  let data = $state<Record<string, Dataset>>({});

  const range = $derived(RANGES.find((r) => r.key === rangeKey) ?? RANGES[0]);
  const slots = $derived(new Map(nodeIds.map((id, i) => [id, i])));
  /* Kept in METRICS order rather than click order, so the stack doesn't
     reshuffle as you toggle things on and off. */
  const chosen = $derived(METRICS.filter((m) => selected.includes(m.key)));

  /* One node: label lines by metric alone. Several: the node has to be in the
     label too, since two nodes' GPU temperature are different lines. */
  const combined = $derived(
    combine(
      chosen.filter((m) => data[m.key]?.x.length).map((m) => ({ metric: m, data: data[m.key] })),
      { labelNodes: nodeIds.length > 1, stepSeconds: parseInt(range.step, 10) || 60 },
    ),
  );

  /* Flagged in the legend: a series with no natural ceiling is scaled to the
     window's own maximum, so its height is relative to itself rather than to a
     fixed reference. Saying so is the difference between a chart that's
     approximate and one that's misleading. */
  const relative = $derived(combined.series.filter((s) => s.scaleMax === null));

  /* Hovered sample, reported up from the chart. The chips double as the legend
     AND the value readout, which is what let the separate legend go: the
     colour and name are already there, so the live number costs no extra
     space. Null when the pointer is off the plot — the chips then show the
     most recent sample instead, so they are never blank. */
  let cursorIdx = $state<number | null>(null);

  function readingFor(metricKey: string): string | null {
    const s = combined.series.find((c) => c.metricKey === metricKey);
    if (!s) return null;
    const at = cursorIdx ?? s.raw.length - 1;
    const v = s.raw[at];
    return v == null ? null : `${v.toFixed(v >= 100 ? 0 : 1)}${s.unit}`;
  }

  function toggle(key: string) {
    const next = selected.includes(key)
      ? selected.filter((k) => k !== key)
      : [...selected, key];
    // Never leave the panel empty — an empty chart area reads as broken
    // rather than as a choice.
    if (!next.length) return;
    selected = next;
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(selected));
    } catch {
      // Selection still works for this session.
    }
  }

  let inflight: AbortController | null = null;

  async function load() {
    inflight?.abort();
    const controller = new AbortController();
    inflight = controller;
    loading = true;
    error = null;

    try {
      // In parallel: these are independent range queries, and doing them in
      // series would make a four-metric view four times slower to appear.
      const results = await Promise.all(
        chosen.map(async (m) => {
          const resp = await fetchHistory(m.key, range.minutes, range.step, controller.signal);
          return [m.key, toColumnar(resp.series)] as const;
        }),
      );
      if (controller.signal.aborted) return;
      const next: Record<string, Dataset> = {};
      for (const [key, cols] of results) {
        next[key] = { x: cols.x, columns: cols.columns, names: cols.names };
      }
      data = next;
    } catch (err) {
      if ((err as Error).name === 'AbortError') return;
      // Named plainly: history failing is a Prometheus problem, and saying so
      // is more useful than a generic "could not load".
      error = (err as Error).message;
      data = {};
    } finally {
      if (inflight === controller) loading = false;
    }
  }

  $effect(() => {
    // Re-runs when the selection or the range changes.
    void selected.join(',');
    void range.key;
    load();
  });

  // Refresh on the range's own timescale rather than on the live tick: a 7-day
  // chart redrawing every 2 seconds would be pointless load on Prometheus.
  $effect(() => {
    const period = Math.max(30_000, (range.minutes * 60_000) / 120);
    const timer = setInterval(load, period);
    return () => clearInterval(timer);
  });
</script>

<section class="panel">
  <header>
    <div class="titles">
      <h2 class="eyebrow">History</h2>
      <!-- The NODE key, shown only when there's more than one node to tell
           apart. Each chart carries its own metric name and swatch below, so
           this legend is about nodes and that one is about metrics. -->
      {#if nodeIds.length > 1}
        <span class="legend">
          {#each nodeIds as id (id)}
            <span class="item">
              <span class="swatch" style:background={nodeColor(slots.get(id) ?? 0)}></span>
              {id}
            </span>
          {/each}
        </span>
      {/if}
    </div>

    <div class="controls">
      <div class="ranges" role="group" aria-label="Time range">
        {#each RANGES as r (r.key)}
          <button
            class="range"
            class:active={r.key === rangeKey}
            aria-pressed={r.key === rangeKey}
            onclick={() => (rangeKey = r.key)}
          >
            {r.label}
          </button>
        {/each}
      </div>
    </div>
  </header>

  <!-- Metric picker. Toggles rather than a multi-select listbox: the whole set
       is small enough to show at once, and seeing which are OFF matters as
       much as which are on. Each carries its own colour, so this doubles as the
       legend for the stack below. -->
  <div class="picker" role="group" aria-label="Metrics">
    {#each METRICS as m (m.key)}
      {@const on = selected.includes(m.key)}
      <button
        class="metric"
        class:on
        aria-pressed={on}
        onclick={() => toggle(m.key)}
      >
        <span class="swatch" style:background={on ? metricColor(m.slot) : 'transparent'}></span>
        {m.label}
        {#if on}
          {@const reading = readingFor(m.key)}
          {#if reading}
            <!-- The absolute value, in its own unit. The plot shows a
                 normalised percentage, so without this the real reading would
                 be nowhere on screen. -->
            <span class="reading num">{reading}</span>
          {/if}
        {/if}
      </button>
    {/each}
  </div>

  {#if error}
    <p class="error">Couldn't load history: {error}</p>
  {:else if !combined.series.length}
    <p class="empty dim">
      {loading ? 'Loading…' : 'No data in this range.'}
    </p>
  {:else}
    <div class="plot" class:loading>
      <CombinedChart
        x={combined.x}
        series={combined.series}
        theme={themeKey}
        oncursor={(idx) => (cursorIdx = idx)}
      />
    </div>

    {#if relative.length}
      <!-- Named rather than quietly scaled: this line's height means something
           different from the others', and a reader comparing peaks deserves to
           know which reference each one uses. -->
      <p class="note dim">
        Scaled to the window's own maximum, so height is relative to itself:
        {relative.map((s) => s.label).join(', ')}. Hover for absolute values.
      </p>
    {/if}
  {/if}
</section>

<style>
  section {
    padding: 12px 16px 12px;
  }

  header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px 16px;
    padding-bottom: 8px;
  }

  .titles {
    display: flex;
    align-items: center;
    gap: 16px;
    flex-wrap: wrap;
  }

  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 12px;
    font-size: 11px;
    color: var(--ink-2);
  }

  .item {
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }

  .swatch {
    width: 8px;
    height: 2px;
    flex: none;
  }

  /* Metric picker ------------------------------------------------------- */

  .picker {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    padding-bottom: 8px;
  }

  .metric {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    padding: 3px 9px;
    border-radius: var(--radius);
    border: 1px solid var(--rule);
    color: var(--ink-muted);
  }

  .metric:hover {
    color: var(--ink-2);
  }

  /* Selected state carries a filled swatch AND a brighter label, so it never
     rests on colour alone — the same reason every status here ships with a
     word. An unselected chip keeps its outline so the set reads as a group of
     options rather than as some labels and some buttons. */
  .metric.on {
    color: var(--ink);
    background: var(--panel-raised);
  }

  .reading {
    color: var(--ink);
    font-size: 10px;
    padding-left: 2px;
  }

  .metric .swatch {
    width: 8px;
    height: 8px;
    border-radius: 2px;
    border: 1px solid var(--rule);
  }

  /* Chart ---------------------------------------------------------------- */

  .plot {
    transition: opacity 150ms ease;
  }

  /* Dim rather than blank while refetching: replacing the chart with a
     spinner would lose the reading you were in the middle of. */
  .plot.loading {
    opacity: 0.6;
  }


  .note {
    font-size: 10px;
    margin: 6px 0 0;
  }





  .empty {
    font-size: 11px;
    /* Holds the chart's height so toggling a metric with no data doesn't make
       the whole stack jump. */
    height: 140px;
    display: flex;
    align-items: center;
  }

  .controls {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .ranges {
    display: flex;
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    overflow: hidden;
  }

  .range {
    font-size: 11px;
    padding: 3px 9px;
    color: var(--ink-muted);
    border-right: 1px solid var(--rule);
  }

  .range:last-child {
    border-right: none;
  }

  .range:hover {
    color: var(--ink);
  }

  .range.active {
    color: var(--ink);
    background: var(--panel-raised);
  }

  .error {
    font-size: 12px;
    color: var(--warning);
    margin: 0;
  }

</style>
