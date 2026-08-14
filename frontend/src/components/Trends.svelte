<script lang="ts">
  /* History — the thing a TUI can't give you.
   *
   * The live view answers "what's happening"; this answers "how did it get
   * here". Both are needed: a GPU at 84°C means something different when it's
   * been climbing for an hour than when it just spiked.
   */
  import TrendChart from './TrendChart.svelte';
  import { METRICS, RANGES, fetchHistory, toColumnar } from '../lib/history';
  import { nodeColor } from '../lib/theme';

  interface Props {
    /** Ordered node ids, so chart colours match the cards. */
    nodeIds: string[];
    /** Changes whenever the theme does, so charts rebuild with the new
     *  canvas colours. */
    themeKey: string;
  }
  const { nodeIds, themeKey }: Props = $props();

  let metricKey = $state(METRICS[0].key);
  let rangeKey = $state(RANGES[0].key);
  let error = $state<string | null>(null);
  let loading = $state(false);

  let x = $state<number[]>([]);
  let columns = $state<number[][]>([]);
  let names = $state<string[]>([]);

  const metric = $derived(METRICS.find((m) => m.key === metricKey) ?? METRICS[0]);
  const range = $derived(RANGES.find((r) => r.key === rangeKey) ?? RANGES[0]);
  const slots = $derived(new Map(nodeIds.map((id, i) => [id, i])));

  let inflight: AbortController | null = null;

  async function load() {
    inflight?.abort();
    const controller = new AbortController();
    inflight = controller;
    loading = true;
    error = null;

    try {
      const resp = await fetchHistory(
        metric.key,
        range.minutes,
        range.step,
        controller.signal,
      );
      const cols = toColumnar(resp.series);
      x = cols.x;
      columns = cols.columns;
      names = cols.names;
    } catch (err) {
      if ((err as Error).name === 'AbortError') return;
      // Named plainly: history failing is a Prometheus problem, and saying so
      // is more useful than a generic "could not load".
      error = (err as Error).message;
      x = [];
      columns = [];
      names = [];
    } finally {
      if (inflight === controller) loading = false;
    }
  }

  $effect(() => {
    // Re-runs when the metric or range changes.
    void metric.key;
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
      <!-- Legend sits with the controls, where it also serves as the node key
           for the whole page. -->
      {#if names.length > 1}
        <span class="legend">
          {#each names as name (name)}
            <span class="item">
              <span class="swatch" style:background={nodeColor(slots.get(name) ?? 0)}></span>
              {name}
            </span>
          {/each}
        </span>
      {/if}
    </div>

    <div class="controls">
      <label class="sr-only" for="metric">Metric</label>
      <select id="metric" bind:value={metricKey}>
        {#each METRICS as m (m.key)}
          <option value={m.key}>{m.label}</option>
        {/each}
      </select>

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

  {#if error}
    <p class="error">Couldn't load history: {error}</p>
  {:else}
    <div class="plot" class:loading>
      <TrendChart
        {x}
        {columns}
        {names}
        {slots}
        unit={metric.unit}
        percent={metric.percent}
        theme={themeKey}
      />
    </div>
  {/if}
</section>

<style>
  section {
    padding: 14px 16px 16px;
  }

  header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 10px 16px;
    padding-bottom: 12px;
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

  .controls {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  select {
    font: inherit;
    font-size: 11px;
    color: var(--ink);
    background: var(--panel-raised);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    padding: 3px 6px;
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

  .plot {
    transition: opacity 150ms ease;
  }

  /* Dim rather than blank while refetching: replacing the chart with a spinner
     would lose the reading you were in the middle of. */
  .plot.loading {
    opacity: 0.6;
  }

  .error {
    font-size: 12px;
    color: var(--warning);
    margin: 0;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
  }
</style>
