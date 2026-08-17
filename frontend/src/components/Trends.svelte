<script lang="ts">
  /* History — the thing a TUI can't give you.
   *
   * The live view answers "what's happening"; this answers "how did it get
   * here". Both are needed: a GPU at 84°C means something different when it's
   * been climbing for an hour than when it just spiked.
   */
  import MetricChart from './MetricChart.svelte';
  import { METRICS, RANGES, fetchHistory, snapGrid, toColumnar } from '../lib/history';
  import { metricColor, nodeColor } from '../lib/theme';

  interface Props {
    /** Ordered node ids, so chart colours match the cards. */
    nodeIds: string[];
    /** Changes whenever the theme does, so charts rebuild with the new
     *  canvas colours. */
    themeKey: string;
  }
  const { nodeIds, themeKey }: Props = $props();

  /* ONE CHART PER METRIC, not one chart per node.
   *
   * Per-node was the obvious reading of "this is designed for a single node"
   * and it is the wrong axis: the chart count would grow with the cluster,
   * which is the very problem being solved. Per-metric is capped at the metric
   * list — eight charts at 32 nodes exactly as at one.
   *
   * It also buys a real axis. Everything used to be normalised onto a shared
   * 0-100% scale purely because %, °C, W, MHz and tok/s cannot share a raw one.
   * With a chart per metric that constraint is gone: 70 on the temperature
   * chart is 70°C.
   *
   * Measured before this change, at four nodes with seven metrics: 28 lines in
   * 7 colours, because colour was the metric — all four nodes' temperature drew
   * the same orange, separable only by hovering. Colour is the NODE now, which
   * is what this card's legend always claimed and never did.
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

  /* Which nodes are drawn. `null` means all of them.
   *
   * NOT PERSISTED, deliberately. Scoping to one node is a question you ask, not
   * a preference you hold — and on a monitoring dashboard a node you forgot you
   * deselected is a node whose history you have quietly stopped watching. The
   * cards and the alerts are unaffected either way, so the blast radius is
   * small, but a filter that cannot outlive the session has none at all.
   */
  let activeNodes = $state<string[] | null>(null);

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

  const isActive = (id: string) => activeNodes === null || activeNodes.includes(id);
  const shownNodes = $derived(nodeIds.filter(isActive));

  /* Charts that actually have something to draw. A metric still loading, or
     with no samples in the window, is held back rather than rendered as an
     empty frame. */
  const drawable = $derived(chosen.filter((m) => data[m.key]?.x.length));

  /** One dataset per chart, filtered to the visible nodes.
   *
   * Filtering here rather than inside the chart keeps the slot map — and so
   * every node's colour — derived from the FULL ordered list. Hiding gx10-b
   * must never repaint gx10-c, which is exactly what would happen if the
   * visible subset became the input to the colouring. */
  const charts = $derived(
    drawable.map((metric) => {
      const d = data[metric.key];
      const keep = d.names.map((n, i) => [n, i] as const).filter(([n]) => isActive(n));
      return {
        metric,
        x: d.x,
        names: keep.map(([n]) => n),
        columns: keep.map(([, i]) => d.columns[i]),
      };
    }),
  );

  /* Up to 4 across, snapping 1 / 2 / 4 — powers of two, the same reasoning as
     the node grid: clusters scale in powers of two, so a 3-wide row is the one
     that strands a row. The two values are separate because the wide count has
     to fall back on a narrower page, and `repeat()` cannot take a `min()`. */
  const cols = $derived(charts.length >= 4 ? 4 : charts.length >= 2 ? 2 : 1);
  const colsMd = $derived(Math.min(cols, 2));

  /** Click solos, click the soloed node again to restore. Shift adds.
   *
   * Solo-first because "what is this one box doing" is the question the node
   * control exists for, and plain per-node toggles would make it seven clicks
   * out of eight. Never empty: an empty plot reads as broken rather than as a
   * choice — the same rule the metric chips already have. */
  function pickNode(id: string, additive: boolean) {
    if (additive) {
      const current = activeNodes ?? [...nodeIds];
      const next = current.includes(id)
        ? current.filter((n) => n !== id)
        : [...current, id];
      activeNodes = next.length ? next : null;
      if (activeNodes && activeNodes.length === nodeIds.length) activeNodes = null;
      return;
    }
    const soloed = activeNodes?.length === 1 && activeNodes[0] === id;
    activeNodes = soloed ? null : [id];
  }

  /* CHIPS ARE LABELS ONLY.
   *
   * They used to carry the live value too, which read well with one or two
   * metrics selected and wrapped onto a second row past that — a control strip
   * whose height grew as you asked the chart for more, pushing the plot down.
   *
   * The absolute readings did not just disappear: the chart now has a floating
   * tooltip on each chart, which costs no layout and cannot reflow. The five
   * readings that are current-state rather than history — GPU, clock, temp,
   * power, CPU — were already on each node's card, and the three that were not
   * (memory %, throughput, pressure %) have been added there.
   */

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
      const step = parseInt(range.step, 10) || 60;
      for (const [key, cols] of results) {
        /* SNAPPED TO THE STEP GRID. Each metric is a separate range query and
           the backend computes its own `end = time.time()`, so parallel
           requests come back on grids offset by milliseconds. That no longer
           breaks a merge — there is no merge — but the charts are now read as a
           grid, and x domains that differ by a fraction of a step put each
           plot's gridlines and crosshair at slightly different pixels. Snapping
           makes the small multiples line up column for column. */
        next[key] = { ...snapGrid(cols.x, cols.columns, step), names: cols.names };
      }
      data = next;
    } catch (err) {
      if ((err as Error).name === 'AbortError') return;
      // Named plainly: history failing is a Prometheus problem, and saying so
      // is more useful than a generic "could not load".
      error = (err as Error).message;
      data = {};
    } finally {
      // Only the most recent request may clear the flag — an older one
      // finishing late must not make a newer in-flight load look done. This is
      // correct only because every request now settles: with no timeout on
      // fetch, a hung request stayed `inflight` forever and nothing could
      // clear the flag. See lib/request.ts.
      if (inflight === controller) loading = false;
    }
  }

  $effect(() => {
    // Re-runs when the selection or the range changes.
    void selected.join(',');
    void range.key;
    load();
  });

  /* Refresh on the range's own timescale rather than on the live tick: a 7-day
   * chart redrawing every 2 seconds would be pointless load on Prometheus.
   *
   * CAPPED, though. Scaling with the range put the 7d refresh 84 minutes out,
   * and that interval is also the only thing that retries after a failed load
   * — so a request that died with the backend left this panel on "Loading…"
   * for an hour and a half while everything else recovered. Five minutes is
   * still cheap for a 7d query and bounds how long a transient failure can
   * show. The real fix is the fetch timeout in lib/request.ts; this bounds the
   * blast radius of anything else that goes wrong. */
  const MAX_REFRESH_MS = 5 * 60_000;

  $effect(() => {
    const period = Math.min(MAX_REFRESH_MS, Math.max(30_000, (range.minutes * 60_000) / 120));
    const timer = setInterval(load, period);
    return () => clearInterval(timer);
  });
</script>

<section class="panel">
  <header>
    <div class="titles">
      <h2 class="eyebrow">History</h2>
      <!-- THE LEGEND IS THE CONTROL. It was decoration before — it mapped
           colours to nodes while every line on the chart was coloured by
           METRIC, so nothing on the plot used these swatches. Now the swatches
           are real and clicking one scopes every chart to that node, which is
           what takes this panel from "which node is different" to "what is this
           one box doing" without a second layout.
           Shown only when there is more than one node: with one node it would
           be a control whose every state looks the same. -->
      {#if nodeIds.length > 1}
        <span class="legend" role="group" aria-label="Nodes">
          {#each nodeIds as id (id)}
            {@const on = isActive(id)}
            <button
              class="item"
              class:off={!on}
              aria-pressed={on}
              title={`Show only ${id} — shift-click to add`}
              onclick={(e) => pickNode(id, e.shiftKey)}
            >
              <span class="swatch" style:background={nodeColor(slots.get(id))}></span>
              {id}
            </button>
          {/each}
          {#if activeNodes}
            <button class="clear" onclick={() => (activeNodes = null)}>
              {shownNodes.length} of {nodeIds.length} · show all
            </button>
          {/if}
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
      </button>
    {/each}
  </div>

  {#if error}
    <p class="error">Couldn't load history: {error}</p>
  {:else if !charts.length}
    <p class="empty dim">
      {loading ? 'Loading…' : 'No data in this range.'}
    </p>
  {:else}
    <div
      class="charts"
      class:loading
      style:--cols={cols}
      style:--cols-md={colsMd}
    >
      {#each charts as c (c.metric.key)}
        <MetricChart
          metric={c.metric}
          x={c.x}
          columns={c.columns}
          names={c.names}
          {slots}
          theme={themeKey}
          syncKey="spark-dash-history"
        />
      {/each}
    </div>
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
    align-items: center;
    gap: 4px 8px;
    font-size: 11px;
    color: var(--ink-2);
  }

  .item {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 2px 6px;
    border-radius: var(--radius);
    color: inherit;
    cursor: pointer;
  }

  .item:hover {
    background: var(--panel-raised);
    color: var(--ink);
  }

  /* A deselected node keeps its swatch at full colour and dims the NAME. The
     swatch is the key to the chart — draining it would make the legend stop
     explaining the lines that are still drawn. */
  .item.off {
    color: var(--ink-muted);
    opacity: 0.65;
  }

  .clear {
    font-size: 10px;
    letter-spacing: 0.04em;
    padding: 2px 7px;
    border-radius: var(--radius);
    border: 1px solid var(--rule);
    color: var(--ink-muted);
    cursor: pointer;
  }

  .clear:hover {
    color: var(--ink);
    border-color: var(--ink-muted);
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


  .metric .swatch {
    width: 8px;
    height: 8px;
    border-radius: 2px;
    border: 1px solid var(--rule);
  }

  /* Small multiples ------------------------------------------------------ */

  .charts {
    display: grid;
    gap: 12px 14px;
    transition: opacity 150ms ease;
    /* minmax(0, …), never a bare 1fr: `1fr` is minmax(auto, 1fr) and a track
       then refuses to be narrower than its content, so a chart's own width
       would push the grid around. Same bug as the section columns. */
    grid-template-columns: minmax(0, 1fr);
  }

  /* Two across once there is room for two readable time axes, and the full
     count only on a wide page. Both counts come from the chart count, so a
     single selected metric still gets the whole width rather than a quarter of
     it. */
  @media (min-width: 760px) {
    .charts {
      grid-template-columns: repeat(var(--cols-md, 1), minmax(0, 1fr));
    }
  }

  @media (min-width: 1500px) {
    .charts {
      grid-template-columns: repeat(var(--cols, 1), minmax(0, 1fr));
    }
  }

  /* Dim rather than blank while refetching: replacing the charts with a
     spinner would lose the reading you were in the middle of. */
  .charts.loading {
    opacity: 0.6;
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
