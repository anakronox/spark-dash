<script lang="ts">
  /* Several metrics on ONE plot, against one 0-100% axis.
   *
   * The axis is a normalised percentage, not a second y-scale. Each series is
   * divided by a fixed ceiling (100°C, 300W, 3003MHz) so the lines share a
   * scale that means something, and the TOOLTIP CARRIES THE REAL READING —
   * "70°C", not "70% of something". That split is what keeps this honest: the
   * shape is comparable at a glance, and the number you'd act on is one hover
   * away and in its own units.
   *
   * Deliberately NOT a dual-axis chart. Two y-scales let a chart imply a
   * correlation purely by where the scaling makes two lines cross, which is
   * the most common way a chart lies.
   *
   * Gridlines are pinned at 0/25/50/75/100 and never auto-scaled: a fixed
   * reference is the whole point of a shared axis, and an auto-fitted one
   * would make a quiet hour look as dramatic as a busy one.
   */
  import uPlot from 'uplot';
  import 'uplot/dist/uPlot.min.css';
  import { onMount } from 'svelte';
  import { chartTheme, metricColor } from '../lib/theme';
  import type { CombinedSeries } from '../lib/history';

  interface Props {
    x: number[];
    series: CombinedSeries[];
    /** Recreates the chart; canvas colours resolve at build time and can't
     *  follow CSS variables afterwards. */
    theme: string;
    height?: number;
    /** Hovered sample index, or null when the pointer leaves the plot. Lets the
     *  caller show live values without a second legend. */
    oncursor?: (idx: number | null) => void;
  }
  const { x, series, theme, height = 200, oncursor }: Props = $props();

  let host = $state<HTMLDivElement | null>(null);
  let chart: uPlot | null = null;
  let width = $state(720);

  /* FLOATING TOOLTIP, not a legend and not the chips.
   *
   * The y axis is normalised to 0-100%, so a plotted height is meaningless on
   * its own: 60% is 2431MHz for the clock and 60°C for temperature. Some
   * readout of the RAW value is therefore not decoration, it is what keeps the
   * chart honest.
   *
   * This used to live on the metric chips above. That cost nothing while two
   * were selected and wrapped them onto a second row past that, which is a
   * layout that grows as you ask the chart for more. Floating over the plot
   * costs no layout at all and cannot reflow anything. */
  let hover = $state<{ left: number; top: number; idx: number } | null>(null);

  const stamp = (ts: number) =>
    new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

  const GRID_AT = [0, 25, 50, 75, 100];

  function build() {
    if (!host) return;
    chart?.destroy();
    chart = null;
    if (!x.length || !series.length) return;

    const t = chartTheme();

    const opts: uPlot.Options = {
      width,
      height,
      /* No built-in legend: it duplicated the metric chips above, which already
         carry each colour and name, and it cost real vertical space on a panel
         that has to sit alongside everything else. The absolute readings it
         used to provide come from the floating tooltip instead — see `hover`. */
      legend: { show: false },
      hooks: {
        setCursor: [
          (u: uPlot) => {
            const idx = u.cursor.idx;
            oncursor?.(idx == null ? null : idx);
            hover =
              idx == null || u.cursor.left == null || u.cursor.left < 0
                ? null
                : { left: u.cursor.left, top: u.cursor.top ?? 0, idx };
          },
        ],
      },
      cursor: { x: true, y: false, points: { show: true } },
      scales: {
        x: { time: true },
        // Pinned. `range` returning a constant is what stops uPlot from
        // auto-fitting to the data.
        y: { range: () => [0, 100] as [number, number] },
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
          splits: () => GRID_AT,
          values: (_u, splits) => splits.map((v) => `${v}%`),
        },
      ],
      series: [
        {},
        ...series.map((s) => ({
          label: s.label,
          stroke: metricColor(s.slot),
          width: 2,
          points: { show: false },
          /* The tooltip shows the RAW value in its own unit. uPlot hands the
             normalised number it plotted, so the real one is looked up by
             index — otherwise every reading here would be a percentage of a
             ceiling the reader can't see. */
          value: (_u: uPlot, _v: number, si: number, di: number | null) => {
            // idx is null when the cursor leaves the plot area.
            const real = di == null ? null : series[si - 1]?.raw[di];
            return real == null ? '—' : `${real.toFixed(1)}${series[si - 1].unit}`;
          },
        })),
      ],
    };

    chart = new uPlot(opts, alignedData(), host);
  }

  function alignedData(): uPlot.AlignedData {
    return [
      x,
      ...series.map((s) => s.scaled as (number | null)[]),
    ] as unknown as uPlot.AlignedData;
  }

  /* What forces a full REBUILD, as opposed to new numbers in the same shape.
   * Series identity, colours and geometry are baked into the uPlot options at
   * construction; the samples are not. */
  const shape = $derived(
    [series.map((s) => `${s.metricKey}:${s.label}:${s.slot}`).join('|'), theme, width, height].join('~'),
  );
  let builtShape = '';

  onMount(() => {
    const ro = new ResizeObserver((entries) => {
      const next = Math.floor(entries[0].contentRect.width);
      if (next > 0 && next !== width) width = next;
    });
    if (host) ro.observe(host);
    return () => {
      ro.disconnect();
      chart?.destroy();
    };
  });

  $effect(() => {
    void x;
    void series;
    const key = shape;

    /* A REFRESH MUST NOT REBUILD THE CHART.
     *
     * The periodic reload changes only the samples. Tearing the plot down and
     * recreating it fires setCursor with a null index, which dismissed the
     * tooltip mid-hover — every 30s on the 1h range. Reading a value off the
     * chart is exactly what the tooltip is for, so losing it on a timer is
     * worse than the chip readout it replaced.
     *
     * setData updates in place and leaves the cursor alone. Rebuild only when
     * the SHAPE changes: different metrics, theme or geometry. */
    if (chart && key === builtShape) {
      chart.setData(alignedData());
      return;
    }
    build();
    builtShape = key;
  });
</script>

<div class="wrap">
  <div class="host" bind:this={host}></div>

  {#if hover}
    <!-- Flipped to the left of the cursor once past halfway, so it never runs
         off the panel and never sits under the pointer. -->
    <div
      class="tip"
      class:flip={hover.left > width / 2}
      style:left="{hover.left}px"
      style:top="{hover.top}px"
      aria-hidden="true"
    >
      <div class="when">{stamp(x[hover.idx])}</div>
      {#each series as s (s.metricKey + s.label)}
        {@const v = s.raw[hover.idx]}
        <div class="row">
          <span class="swatch" style:background={metricColor(s.slot)}></span>
          <span class="name">{s.label}</span>
          <span class="val num">{v == null ? '—' : `${v.toFixed(v >= 100 ? 0 : 1)}${s.unit}`}</span>
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .wrap {
    position: relative;
    width: 100%;
  }

  .host {
    width: 100%;
  }

  .tip {
    position: absolute;
    z-index: 2;
    /* Offset from the cursor rather than centred on it: under the pointer the
       value you are pointing at is the one you cannot read. */
    transform: translate(12px, -50%);
    pointer-events: none;
    background: var(--panel);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    padding: 6px 8px;
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

  .tip .name {
    color: var(--ink-muted);
  }

  .tip .val {
    margin-left: auto;
    color: var(--ink);
    font-variant-numeric: tabular-nums;
  }

  /* uPlot ships light-mode defaults; these follow the page. */



  :global(.u-cursor-x),
  :global(.u-cursor-y) {
    border-color: var(--ink-muted) !important;
  }
</style>
