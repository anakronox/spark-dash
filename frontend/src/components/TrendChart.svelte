<script lang="ts">
  /* Time-series chart.
   *
   * uPlot rather than a heavier library: it redraws fast, weighs ~40KB, and
   * this page is already carrying a live WebSocket.
   *
   * Conventions this follows deliberately:
   *  - One y-axis, always. Two scales on one plot is the single most common
   *    way to make a chart lie about correlation.
   *  - Series colour comes from the node's identity slot, so a line and its
   *    card are the same colour. Colour follows the node, never its position.
   *  - Grid and axes recede; the data is the only thing at full contrast.
   *  - Gaps are drawn as gaps. A line interpolated through an outage would
   *    invent data for the exact window you're investigating.
   */
  import uPlot from 'uplot';
  import 'uplot/dist/uPlot.min.css';
  import { onMount } from 'svelte';
  import { chartTheme, nodeColor } from '../lib/theme';

  interface Props {
    x: number[];
    columns: number[][];
    names: string[];
    /** node id -> categorical slot, shared with the node cards. */
    slots: Map<string, number>;
    unit: string;
    percent?: boolean;
    /** Recreates the chart; canvas colours can't follow CSS variables. */
    theme: 'dark' | 'light';
    height?: number;
  }

  const { x, columns, names, slots, unit, percent = false, theme, height = 200 }: Props =
    $props();

  let host = $state<HTMLDivElement | null>(null);
  let chart: uPlot | null = null;
  let width = $state(720);

  function build() {
    if (!host) return;
    chart?.destroy();
    chart = null;
    if (!x.length) return;

    const t = chartTheme();

    const opts: uPlot.Options = {
      width,
      height,
      // The built-in legend is a table below the plot; this page shows its own
      // legend inline with the controls, where it doubles as the series key.
      legend: { show: false },
      cursor: {
        // Crosshair follows the x position across every series, so values are
        // compared at the same instant rather than wherever the pointer lands.
        x: true,
        y: false,
        points: { size: 6 },
      },
      scales: {
        x: { time: true },
        y: percent ? { range: [0, 100] } : { auto: true },
      },
      axes: [
        {
          stroke: t.inkMuted,
          grid: { stroke: t.grid, width: 1 },
          ticks: { stroke: t.grid, width: 1 },
          font: '10px ui-monospace, monospace',
        },
        {
          stroke: t.inkMuted,
          grid: { stroke: t.grid, width: 1 },
          ticks: { stroke: t.grid, width: 1 },
          font: '10px ui-monospace, monospace',
          size: 52,
          values: (_u, splits) => splits.map((v) => `${v}${unit}`),
        },
      ],
      series: [
        {},
        ...names.map((name) => ({
          label: name,
          stroke: nodeColor(slots.get(name) ?? 0),
          width: 2,
          points: { show: false },
          // Values are read from the tooltip, not from labels on every point.
          value: (_u: uPlot, v: number | null) =>
            v == null ? '—' : `${v.toFixed(1)}${unit}`,
        })),
      ],
    };

    chart = new uPlot(opts, [x, ...columns] as uPlot.AlignedData, host);
  }

  onMount(() => {
    const observer = new ResizeObserver((entries) => {
      const next = Math.floor(entries[0].contentRect.width);
      if (next > 0 && next !== width) width = next;
    });
    if (host) observer.observe(host);
    return () => {
      observer.disconnect();
      chart?.destroy();
      chart = null;
    };
  });

  // Rebuild on theme change (canvas colours are baked in at construction) and
  // on a change of series identity. A data-only update uses setData, which is
  // far cheaper than tearing the chart down.
  let lastSignature = '';
  $effect(() => {
    const signature = `${theme}|${unit}|${percent}|${names.join(',')}|${width}|${height}`;
    if (signature !== lastSignature) {
      lastSignature = signature;
      build();
    } else if (chart && x.length) {
      chart.setData([x, ...columns] as uPlot.AlignedData);
    } else if (chart && !x.length) {
      build();
    }
  });
</script>

<div class="chart" bind:this={host}></div>

{#if !x.length}
  <p class="empty">
    No data in this window yet. Prometheus needs to have been running across it.
  </p>
{/if}

<style>
  .chart {
    width: 100%;
  }

  .empty {
    font-size: 12px;
    color: var(--ink-2);
    margin: 0;
    padding: 12px 0 4px;
  }

  /* uPlot ships light-mode defaults; these follow the page instead. */
  :global(.u-cursor-x),
  :global(.u-cursor-y) {
    border-color: var(--ink-muted) !important;
  }

  :global(.u-tooltip) {
    background: var(--panel-raised);
  }
</style>
