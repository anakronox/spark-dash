<script lang="ts">
  /* History — the thing a TUI can't give you.
   *
   * The live view answers "what's happening"; this answers "how did it get
   * here". Both are needed: a GPU at 84°C means something different when it's
   * been climbing for an hour than when it just spiked.
   */
  import MetricChart from './MetricChart.svelte';
  import { METRICS, RANGES, fetchAnnotations, fetchHistory, snapGrid, toColumnar } from '../lib/history';
  import type { Annotation } from '../lib/history';
  import { nodeColor } from '../lib/theme';

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
  const EVENTS_KEY = 'spark-dash.trend-events.v1';

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

  /* Events drawn on the charts. ON by default: the whole point is that a dip
     arrives with its candidate explanation attached, and a correlation layer
     nobody switches on explains nothing. Off is for when the marks are in the
     way of reading a shape. */
  let showEvents = $state(readEvents());
  let annotations = $state<Annotation[]>([]);

  function readEvents(): boolean {
    try {
      return localStorage.getItem(EVENTS_KEY) !== '0';
    } catch {
      return true;
    }
  }

  function toggleEvents() {
    showEvents = !showEvents;
    try {
      localStorage.setItem(EVENTS_KEY, showEvents ? '1' : '0');
    } catch {
      // Still applied for this session.
    }
  }

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

  /* Every metric that has LOADED gets a chart, including one that came back
     empty.
     It used to be `data[m.key]?.x.length`, which silently dropped a metric with
     no samples — so selecting Throughput on an idle cluster toggled the chip
     and changed nothing on the page, which is a control that does nothing.
     Worse, "no throughput in this window" is itself a reading: it means nothing
     was serving. An absent chart cannot say that; an empty one can.
     Still keyed on the entry EXISTING rather than on its length, so a metric
     mid-fetch is held back instead of flashing an empty frame before its data
     lands. */
  const drawable = $derived(chosen.filter((m) => data[m.key]));

  /** The nodes the charts can actually draw.
   *
   * THE LEGEND IS THE KEY TO THE LINES, SO IT LISTS THE LINES. It used to list
   * `nodeIds` — the LIVE inventory — while the lines come from Prometheus, and
   * those two sets are not the same. Clicking a node with no history then
   * filtered every series out and left each chart as a caption with no plot,
   * which reads as the panel having broken. A node can be live with no history
   * for perfectly ordinary reasons: just added to `cluster.yml`, or being
   * scraped for less than the window.
   *
   * Inventory order first so colours stay stable, then anything history knows
   * that the inventory does not — a node recently removed still has samples for
   * the rest of the window, and it IS drawn, so it belongs in the key.
   */
  const plotted = $derived.by(() => {
    const seen = new Set<string>();
    for (const m of drawable) for (const n of data[m.key].names) seen.add(n);
    return [
      ...nodeIds.filter((id) => seen.has(id)),
      ...[...seen].filter((n) => !nodeIds.includes(n)).sort(),
    ];
  });

  /** Every node the legend lists: the inventory, plus anything history knows
   *  that the inventory does not.
   *
   * DERIVING THE LEGEND FROM `plotted` ALONE WAS AN OVER-CORRECTION. It began as
   * the fix for a real bug — the legend listed live nodes, and clicking one with
   * no history filtered every series out and blanked the charts. Hiding those
   * nodes stopped the blanking and also removed the control: on a cluster where
   * only some nodes have samples, the node toggles simply vanished, which reads
   * as the feature having been taken away.
   *
   * The control belongs on the page. A node that cannot be plotted is listed
   * and disabled, saying why, rather than hidden — the same call as an `err`
   * column that comes back when it has something to say, and as naming a node
   * with no history instead of quietly dropping it. */
  const legendNodes = $derived([...new Set([...nodeIds, ...plotted])]);

  const hasHistory = (id: string) => plotted.includes(id);

  /** The selection, self-healing.
   *
   * A refresh can retire the very node that was soloed — it drops out of the
   * window, or leaves the cluster. Holding a selection that matches nothing
   * would blank the panel with no way back except a control the reader cannot
   * see the point of, so a selection that no longer names anything plottable
   * collapses back to "all". */
  const active = $derived.by(() => {
    if (!activeNodes) return null;
    const kept = activeNodes.filter((n) => plotted.includes(n));
    return kept.length ? kept : null;
  });

  const isActive = (id: string) => active === null || active.includes(id);
  const shownNodes = $derived(plotted.filter(isActive));

  /** The time axis of whichever metric did return data.
   *
   * Lent to any metric that came back empty so its chart still spans the same
   * window as its neighbours. Without it an empty chart has no x domain, and
   * uPlot would draw it over a different (or absent) time range — a grid of
   * small multiples whose axes disagree is worse than one with a gap in it. */
  const fallbackX = $derived(drawable.map((m) => data[m.key]).find((d) => d.x.length)?.x ?? []);

  /** True when ANY selected metric has samples. When none do there is nothing
   *  to put an axis against, and the panel says so once rather than eight
   *  times. */
  const anyData = $derived(fallbackX.length > 0);

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
        // An empty metric borrows the window from one that isn't, so its axes
        // match the charts beside it and it reads as "nothing happened here"
        // rather than as a broken frame.
        x: d.x.length ? d.x : fallbackX,
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
      const current = active ?? [...plotted];
      const next = current.includes(id)
        ? current.filter((n) => n !== id)
        : [...current, id];
      activeNodes = next.length ? next : null;
      // Everything selected IS "all" — kept as null so the count and the
      // "show all" control do not linger over a selection of everything.
      if (activeNodes && activeNodes.length === plotted.length) activeNodes = null;
      return;
    }
    const soloed = active?.length === 1 && active[0] === id;
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
      /* Fetched with the SAME window and step as the metrics, so a mark lands
         on the sample it explains rather than between two of them. Failure is
         non-fatal and deliberately so: annotations are context, and losing
         them must not cost the reader the chart. */
      try {
        annotations = await fetchAnnotations(range.minutes, range.step, controller.signal);
      } catch {
        annotations = [];
      }

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
      {#if legendNodes.length > 1}
        <span class="legend" role="group" aria-label="Nodes">
          {#each legendNodes as id (id)}
            {@const plottable = hasHistory(id)}
            {@const on = plottable && isActive(id)}
            <button
              class="item"
              class:off={!on}
              class:absent={!plottable}
              disabled={!plottable}
              aria-pressed={on}
              title={plottable
                ? `Show only ${id} — shift-click to add`
                : `${id} has no samples in this range — live, but nothing to plot yet`}
              onclick={(e) => pickNode(id, e.shiftKey)}
            >
              <span class="swatch" style:background={nodeColor(slots.get(id))}></span>
              {id}
            </button>
          {/each}
          {#if active}
            <button class="clear" onclick={() => (activeNodes = null)}>
              {shownNodes.length} of {plotted.length} · show all
            </button>
          {/if}
        </span>
      {/if}
    </div>

    <div class="controls">
      <!-- Beside the range, because the two together decide what the charts
           are showing. The count is on the control: an events layer with
           nothing in the window otherwise reads as broken rather than quiet. -->
      <button
        class="events"
        class:on={showEvents}
        aria-pressed={showEvents}
        title="Mark alerts, cold starts and agent deploys on the charts"
        onclick={toggleEvents}
      >events{annotations.length ? ` · ${annotations.length}` : ''}</button>

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
        <!-- Filled when on, hollow when off — a STATE mark, not an identity
             colour. It used to be the metric's own hue, which was right when
             every metric was a line on one shared plot. It is not any more:
             each metric has its own chart and the lines in it are coloured by
             NODE, so a per-metric hue here named a colour that appears nowhere
             on the page. Same fault the node legend had. -->
        <span class="swatch" class:on></span>
        {m.label}
      </button>
    {/each}
  </div>

  {#if error}
    <p class="error">Couldn't load history: {error}</p>
  {:else if !charts.length || !anyData}
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
          annotations={showEvents ? annotations : []}
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

  /* Listed but unplottable: in the cluster, with no samples in this window.
     NOT struck through — that was the first attempt and it is wrong here. A
     struck-out node name on a monitoring dashboard reads as dead or removed,
     which is the opposite of what this means: the node is up, it simply has no
     history yet. Usually because it was just added.
     "Off" and "cannot be turned on" still have to be distinguishable, and they
     are, on the swatch: a deselected node keeps its identity colour, an
     unplottable one goes neutral. The reason is on the title. */
  .item.absent {
    cursor: default;
    opacity: 0.5;
  }

  .item.absent:hover {
    background: none;
    color: var(--ink-muted);
  }

  .item.absent .swatch {
    background: var(--rule) !important;
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
    border: 1px solid var(--ink-muted);
    background: transparent;
  }

  /* Fill plus the brighter label and panel behind it, so selection is never
     carried by one channel alone. */
  .metric .swatch.on {
    background: currentColor;
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

  .events {
    font-size: 11px;
    padding: 3px 9px;
    border-radius: var(--radius);
    border: 1px solid var(--rule);
    color: var(--ink-muted);
    cursor: pointer;
  }

  .events:hover {
    color: var(--ink);
  }

  .events.on {
    color: var(--ink);
    background: var(--panel-raised);
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
