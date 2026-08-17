<script lang="ts">
  /* ONE metric, one line per node, in the metric's own units.
   *
   * The panel used to draw every metric on a single plot, which forced a
   * normalised 0-100% axis: a 2400MHz clock and a 70°C temperature cannot share
   * a raw scale, and a second y-axis is worse still — two scales let a chart
   * imply a correlation purely by where the scaling makes the lines cross.
   *
   * Splitting by metric removes the constraint rather than working around it.
   * One unit per chart means a REAL axis: 70 on the temperature chart is 70°C,
   * not 70% of a ceiling the reader cannot see. What the tooltip used to have
   * to rescue, the axis now just says.
   *
   * COLOUR IS THE NODE here, where it used to be the metric. That is the whole
   * point of the split: with one metric per chart the metric is the title, so
   * colour is free to carry the thing being compared — and it matches the node
   * cards, which is what the legend always claimed and never did.
   */
  import uPlot from 'uplot';
  import 'uplot/dist/uPlot.min.css';
  import { onDestroy } from 'svelte';
  import { chartTheme, nodeColor } from '../lib/theme';
  import type { MetricSpec } from '../lib/history';

  interface Props {
    metric: MetricSpec;
    x: number[];
    /** One column per node, already filtered to the visible ones. */
    columns: (number | null)[][];
    /** Node id per column, same order. */
    names: string[];
    /** Identity slot per node id, derived from the FULL node list — so hiding
     *  one node never repaints another. */
    slots: Map<string, number>;
    /** Rebuilds the chart: canvas colours resolve at build time and cannot
     *  follow CSS variables afterwards. */
    theme: string;
    /** Shared across the small multiples, so one crosshair moves them all. */
    syncKey: string;
    height?: number;
  }
  const { metric, x, columns, names, slots, theme, syncKey, height = 132 }: Props = $props();

  let host = $state<HTMLDivElement | null>(null);
  let chart: uPlot | null = null;
  let width = $state(360);
  let hover = $state<{ left: number; top: number; idx: number } | null>(null);

  /* Is the pointer over THIS chart, as opposed to a synced sibling?
   *
   * Cursor sync fires setCursor on every chart in the group, which is what
   * makes the crosshairs move together — and also meant all eight rendered
   * their own tooltip at once, a screenful of overlapping boxes from a single
   * hover. The crosshair belongs on every chart; the numbers belong on the one
   * being pointed at. */
  let pointerHere = $state(false);

  const stamp = (ts: number) =>
    new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

  /** Where the axis tops out.
   *
   * FIXED per metric, not fitted to the window. A fitted axis makes a quiet
   * hour look exactly like a busy one — a 2°C wobble and a 40°C climb draw the
   * same shape — and it would also rescale every chart the moment a node was
   * toggled off, which is movement under the reader's cursor in response to an
   * unrelated action.
   *
   * Throughput has no natural ceiling, so it alone fits to its data. It is
   * flagged in the caption rather than left to look like the others.
   */
  const ceiling = $derived(metric.percent ? 100 : (metric.scaleMax ?? null));

  const fmt = (v: number) => (Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(1));

  /** Room for the widest tick this axis can print.
   *
   * A flat 44px clipped the clock chart to "20MHz" where it meant "3003MHz" —
   * an axis that silently truncates its own numbers is worse than no axis. Sized
   * from the actual widest label: the ceiling, formatted, plus the unit. */
  const axisWidth = $derived(
    Math.max(44, `${fmt(ceiling ?? 1000)}${metric.unit}`.length * 6.4 + 16),
  );

  function build() {
    if (!host) return;
    chart?.destroy();
    chart = null;
    if (!x.length || !columns.length) return;

    const t = chartTheme();
    const fixed = ceiling;

    const opts: uPlot.Options = {
      width,
      height,
      // The card's own legend names the nodes once for all eight charts;
      // repeating it under each would cost more vertical space than the plots.
      legend: { show: false },
      // Sync is what makes small multiples readable: reading eight charts at
      // one instant is the entire reason for splitting them up.
      cursor: {
        x: true,
        y: false,
        points: { show: true },
        sync: { key: syncKey },
      },
      hooks: {
        setCursor: [
          (u: uPlot) => {
            const idx = u.cursor.idx;
            hover =
              idx == null || u.cursor.left == null || u.cursor.left < 0
                ? null
                : { left: u.cursor.left, top: u.cursor.top ?? 0, idx };
          },
        ],
      },
      scales: {
        x: { time: true },
        y: fixed
          ? // A constant `range` is what stops uPlot auto-fitting.
            { range: () => [0, fixed] as [number, number] }
          : // Still anchored at zero: a throughput chart floated off the
            // baseline exaggerates every wiggle into a cliff.
            { range: (_u, _min, max) => [0, max > 0 ? max : 1] as [number, number] },
      },
      axes: [
        {
          stroke: t.axis,
          grid: { stroke: t.grid, width: 1 },
          ticks: { stroke: t.grid },
          font: '10px ui-monospace, monospace',
        },
        {
          stroke: t.axis,
          grid: { stroke: t.grid, width: 1 },
          ticks: { stroke: t.grid },
          font: '10px ui-monospace, monospace',
          size: axisWidth,
          // The unit on the tick, since there is only one unit per chart now
          // and it is the thing that makes the number readable.
          values: (_u, splits) => splits.map((v) => `${fmt(v)}${metric.unit}`),
        },
      ],
      series: [
        {},
        ...names.map((name) => ({
          label: name,
          stroke: nodeColor(slots.get(name)),
          width: 2,
          points: { show: false },
        })),
      ],
    };

    chart = new uPlot(opts, alignedData(), host);
  }

  function alignedData(): uPlot.AlignedData {
    return [x, ...columns] as unknown as uPlot.AlignedData;
  }

  /* What forces a rebuild rather than new numbers in the same shape. Series
     identity, colours and geometry are baked into the options at construction;
     the samples are not. Node visibility is in here because toggling a node
     adds or removes a series. */
  const shape = $derived(
    [metric.key, names.join('|'), theme, width, height, axisWidth].join('~'),
  );
  let builtShape = '';

  /* Keyed on `host` rather than run once on mount: the plot is replaced by a
     placeholder when there is nothing to draw, so the element this observes
     comes and goes over the component's life. An observer attached once at
     mount would be watching a node that no longer exists by the time data
     arrives, and the chart would keep its initial guessed width forever. */
  $effect(() => {
    const el = host;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const next = Math.floor(entries[0].contentRect.width);
      if (next > 0 && next !== width) width = next;
    });
    ro.observe(el);
    return () => ro.disconnect();
  });

  onDestroy(() => chart?.destroy());

  $effect(() => {
    void x;
    void columns;
    const key = shape;
    /* A REFRESH MUST NOT REBUILD. The periodic reload changes only the samples,
       and tearing the plot down fires setCursor with a null index — which
       dismissed the tooltip mid-hover, every 30s on the 1h range. setData
       updates in place and leaves the cursor alone. */
    if (chart && key === builtShape) {
      chart.setData(alignedData());
      return;
    }
    build();
    builtShape = key;
  });
</script>

<figure class="chart">
  <figcaption>
    <span class="name">{metric.label}</span>
    {#if !ceiling}
      <!-- Named rather than quietly fitted: this chart's height means something
           different from its neighbours', and a reader comparing peaks across
           the grid deserves to know which one uses its own reference. -->
      <span class="dim rel">relative</span>
    {/if}
  </figcaption>

  {#if !x.length}
    <!-- LOADED AND EMPTY, which is not the same as absent. A metric with no
         samples used to be dropped from the grid entirely, so its chip toggled
         nothing and the reader was left wondering whether the control worked.
         It also threw away a reading: no throughput in the window means nothing
         was serving, and only a frame that is present can say so. -->
    <p class="blank dim" style:height="{height}px">No samples in this range.</p>
  {:else if !names.length}
    <p class="blank dim" style:height="{height}px">No samples for the selected nodes.</p>
  {:else}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div
    class="wrap"
    onpointerenter={() => (pointerHere = true)}
    onpointerleave={() => (pointerHere = false)}
  >
    <div class="host" bind:this={host}></div>

    {#if hover && pointerHere}
      <div
        class="tip"
        class:flip={hover.left > width / 2}
        style:left="{hover.left}px"
        style:top="{hover.top}px"
        aria-hidden="true"
      >
        <div class="when">{stamp(x[hover.idx])}</div>
        {#each names as name, i (name)}
          {@const v = columns[i]?.[hover.idx]}
          <div class="row">
            <span class="swatch" style:background={nodeColor(slots.get(name))}></span>
            <span class="who">{name}</span>
            <span class="val num">{v == null ? '—' : `${fmt(v)}${metric.unit}`}</span>
          </div>
        {/each}
      </div>
    {/if}
  </div>
  {/if}
</figure>

<style>
  .chart {
    margin: 0;
    min-width: 0;
  }

  figcaption {
    display: flex;
    align-items: baseline;
    gap: 6px;
    padding: 0 0 2px 2px;
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-muted);
  }

  /* Not bold: the CARD's title is the bold one, and a grid of eight bold
     captions would compete with it rather than sit under it. */
  .name {
    color: var(--ink-2);
  }

  .rel {
    font-size: 9px;
    letter-spacing: 0.04em;
    text-transform: none;
  }

  .wrap {
    position: relative;
    width: 100%;
  }

  /* Holds exactly the plot's height, so a metric going empty on a refresh does
     not resize the grid around it. */
  .blank {
    display: flex;
    align-items: center;
    margin: 0;
    font-size: 11px;
    border: 1px dashed var(--rule);
    border-radius: var(--radius);
    padding: 0 10px;
    box-sizing: border-box;
  }

  .host {
    width: 100%;
  }

  .tip {
    position: absolute;
    z-index: 3;
    /* Offset from the cursor: under the pointer, the value you are pointing at
       is the one you cannot read. */
    transform: translate(12px, -50%);
    pointer-events: none;
    background: var(--panel);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    padding: 5px 7px;
    font-size: 11px;
    white-space: nowrap;
    box-shadow: 0 2px 8px rgb(0 0 0 / 0.25);
  }

  .tip.flip {
    transform: translate(calc(-100% - 12px), -50%);
  }

  .tip .when {
    color: var(--ink-muted);
    font-size: 10px;
    margin-bottom: 3px;
  }

  .tip .row {
    display: flex;
    align-items: baseline;
    gap: 6px;
  }

  .tip .swatch {
    width: 8px;
    height: 8px;
    border-radius: 2px;
    flex: none;
  }

  .tip .who {
    color: var(--ink-muted);
  }

  .tip .val {
    margin-left: auto;
    color: var(--ink);
    font-variant-numeric: tabular-nums;
  }

  :global(.u-cursor-x),
  :global(.u-cursor-y) {
    border-color: var(--ink-muted) !important;
  }
</style>
