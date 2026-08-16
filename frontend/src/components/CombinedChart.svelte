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
         that has to sit alongside everything else.
         The live values it used to provide are reported UPWARDS instead (see
         `oncursor`) and rendered on those same chips — so hovering still gives
         the absolute reading in its own unit, which is what keeps a normalised
         axis honest, without a second legend to read. */
      legend: { show: false },
      hooks: {
        setCursor: [
          (u: uPlot) => {
            const idx = u.cursor.idx;
            oncursor?.(idx == null ? null : idx);
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

    const data: uPlot.AlignedData = [
      x,
      ...series.map((s) => s.scaled as (number | null)[]),
    ] as unknown as uPlot.AlignedData;

    chart = new uPlot(opts, data, host);
  }

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
    // Rebuild on any of these.
    void x;
    void series;
    void theme;
    void width;
    build();
  });
</script>

<div class="host" bind:this={host}></div>

<style>
  .host {
    width: 100%;
  }

  /* uPlot ships light-mode defaults; these follow the page. */



  :global(.u-cursor-x),
  :global(.u-cursor-y) {
    border-color: var(--ink-muted) !important;
  }
</style>
