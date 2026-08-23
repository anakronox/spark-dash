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
  import { chartTheme, cssVar, nodeColor } from '../lib/theme';
  import { bitRate, siScale } from '../lib/format';
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
    /** Events to mark on the time axis. The same list for every chart — they
     *  share an x axis, so an instant lines up across the whole grid. */
    annotations?: { ts: number; kind: string; label: string; node: string | null }[];
    height?: number;
    /** Colour every line by THIS id rather than by its own name.
     *
     * The history grid is one chart per metric with one line per node, so a
     * line's name IS its identity and the default works. The network grid is
     * one chart per INTERFACE, where the lines are receive and transmit — two
     * views of one node's wire, not two nodes. Their names key nothing in
     * `slots`, and colouring by them would fall through to no colour at all.
     *
     * A single id rather than one per column because that is the shape of the
     * thing: a chart of one interface has exactly one owner. */
    identity?: string;
  }
  const {
    metric,
    x,
    columns,
    names,
    slots,
    theme,
    syncKey,
    annotations = [],
    height = 132,
    identity,
  }: Props = $props();

  /** The line colour for a named column. */
  const colourOf = (name: string) => nodeColor(slots.get(identity ?? name));

  /** Dash pattern for a named column, empty for solid.
   *
   * DIRECTION BY DASH, NOT BY HUE. Colour already carries the node here, and
   * on a grid of small multiples that is the property worth keeping constant:
   * every chart belonging to one box reads as a group at a glance. Spending
   * hue on direction instead would break that AND collide with the node
   * palette, which is deliberately kept clear of the status colours.
   *
   * [5, 3] rather than a finer dash: at 132px tall with a 2px stroke, a
   * [2, 2] pattern reads as a lighter solid line rather than as a dashed one.
   */
  const dashOf = (name: string) => (metric.dashed === name ? [5, 3] : []);

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

  /** Annotations within half a sample of the hovered instant.
   *
   * Keyed off the sample spacing rather than a fixed number of seconds, so it
   * stays "the event at this point on the chart" at a 60s step and at a 3600s
   * one alike. */
  function nearbyAnnotations(ts: number) {
    if (!annotations.length || x.length < 2) return [];
    const tolerance = Math.abs(x[1] - x[0]) / 2;
    return annotations.filter((a) => Math.abs(a.ts - ts) <= tolerance);
  }

  const fmt = (v: number) =>
    metric.si ? bitRate(v) : `${Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(1)}${metric.unit}`;

  /* 11px, up from 10. These are eight small charts rather than one large one,
     so their axes are the densest type on the page and were also the smallest.
     Monospace, so the digits stay in tabular columns down the axis. */
  const AXIS_FONT = '11px ui-monospace, monospace';

  /** Gap between the widest tick label and the plot's edge. */
  const AXIS_PAD = 14;

  /* One offscreen context, reused. Text measurement needs the real font, not a
     characters-times-a-constant estimate — see axisWidth. */
  let measurer: CanvasRenderingContext2D | null = null;
  function textWidth(str: string): number {
    measurer ??= document.createElement('canvas').getContext('2d');
    if (!measurer) return str.length * 7;
    measurer.font = AXIS_FONT;
    return measurer.measureText(str).width;
  }

  /** Formats a tick the way the axis will.
   *
   * DECIMALS ONLY WHEN THE SCALE IS SMALL. "50.0%" carries a decimal that says
   * nothing — a tenth of a percent is far below what this axis resolves — and
   * it is the same decimal that made the MIDDLE label wider than the ceiling.
   * Throughput genuinely needs it, since its whole scale can be 0 to 1. */
  function ticks(values: number[]): string[] {
    /* SI axes take one divisor for the whole axis, from the top of the scale.
       Scaling each label on its own produces 0 / 500k / 1M / 1.5M / 2M, where
       the reader re-anchors at every gridline to see that the spacing is even.
       The prefix rides on the unit so the axis still says what it measures. */
    if (metric.si) {
      const { div, prefix } = siScale(Math.max(...values.map(Math.abs), 1));
      const fine = div > 1 && values.some((v) => v !== 0 && Math.abs(v) / div < 10);
      return values.map(
        (v) => `${(v / div).toFixed(fine ? 1 : 0)}${prefix}${metric.unit}`,
      );
    }
    const fine = values.some((v) => v !== 0 && Math.abs(v) < 10);
    return values.map((v) => `${fine ? v.toFixed(1) : v.toFixed(0)}${metric.unit}`);
  }

  /** How wide the y-axis gutter must be, MEASURED.
   *
   * Three attempts, and the first two guessed. A flat 44px turned "3003MHz"
   * into "20MHz". Sizing from the CEILING's label then clipped "50.0°C",
   * because the widest label is usually NOT the ceiling: a middle split gains a
   * decimal the top of the scale does not, so "50.0°C" beats "100°C".
   *
   * Passing uPlot a `size` CALLBACK looked like the clean answer — it hands you
   * the formatted values — but it is called before those exist, so it returned
   * bare padding and clipped every chart on the page. Measuring the labels this
   * axis can actually print, in the font that will draw them, needs no callback
   * and no guess.
   */
  const axisWidth = $derived.by(() => {
    // The auto-fitted case has no ceiling, so take the tallest sample instead —
    // a throughput axis reaching 1500 needs room for "1500tok/s".
    const top =
      ceiling ??
      Math.max(1, ...columns.flat().filter((v): v is number => v != null && isFinite(v)));
    const candidates = [0, top / 4, top / 2, (top * 3) / 4, top];
    return Math.ceil(Math.max(...ticks(candidates).map(textWidth))) + AXIS_PAD;
  });

  /** How an annotation is drawn.
   *
   * An alert takes the STATUS palette, which is reserved for exactly this and
   * so cannot collide with node identity — those come from `--chart-N`. The
   * other two are not status and must not borrow its colours: a deploy is not
   * a warning. They stay recessive and are told apart by dash, with the label
   * on hover doing the real work.
   *
   * Recessive on purpose. These are context FOR the data, not data — an
   * annotation layer that competes with the lines has taken over the chart it
   * was meant to explain.
   */
  const MARK: Record<string, { token: string; dash: number[]; alpha: number }> = {
    alert: { token: '--critical', dash: [], alpha: 0.65 },
    'cold-start': { token: '--ink-2', dash: [4, 3], alpha: 0.5 },
    deploy: { token: '--ink-muted', dash: [1, 3], alpha: 0.5 },
  };

  function drawAnnotations(u: uPlot) {
    if (!annotations.length) return;
    const lo = u.scales.x.min ?? -Infinity;
    const hi = u.scales.x.max ?? Infinity;
    const ctx = u.ctx;

    ctx.save();
    // Clipped to the plot area, or a mark just outside the window paints over
    // the axis labels.
    ctx.beginPath();
    ctx.rect(u.bbox.left, u.bbox.top, u.bbox.width, u.bbox.height);
    ctx.clip();

    for (const a of annotations) {
      if (a.ts < lo || a.ts > hi) continue;
      const style = MARK[a.kind] ?? MARK.deploy;
      ctx.strokeStyle = cssVar(style.token);
      ctx.globalAlpha = style.alpha;
      ctx.lineWidth = 1;
      ctx.setLineDash(style.dash);
      const px = Math.round(u.valToPos(a.ts, 'x', true)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(px, u.bbox.top);
      ctx.lineTo(px, u.bbox.top + u.bbox.height);
      ctx.stroke();
    }
    ctx.restore();
  }

  function build() {
    if (!host) return;
    chart?.destroy();
    chart = null;
    if (!x.length) return;

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
        // BENEATH the series, not over them. `drawClear` fires after the canvas
        // is cleared and before anything is plotted, so the data always sits on
        // top of its own annotations.
        drawClear: [drawAnnotations],
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
          font: AXIS_FONT,
        },
        {
          stroke: t.axis,
          grid: { stroke: t.grid, width: 1 },
          ticks: { stroke: t.grid },
          font: AXIS_FONT,
          size: axisWidth,
          /* The unit on the tick, since there is only one unit per chart now
             and it is what makes the number readable.
             DECIMALS ONLY WHEN THE SCALE IS SMALL. "50.0%" and "0.0°C" carry a
             decimal that says nothing — the tenth of a percent is below what
             the axis can resolve — and it is the same decimal that made the
             middle label wider than the ceiling. Throughput does need it, since
             its whole scale can be 0 to 1. */
          values: (_u, splits) => ticks(splits),
        },
      ],
      /* A metric with no samples still gets its axes, gridlines and time span
         — an EMPTY CHART, not a message where a chart should be. The axes are
         doing real work even with no line on them: they say what the scale is,
         and an empty 0-300W plot reads as "nothing drew power" rather than as a
         panel that failed. uPlot needs at least one y series to lay a plot out,
         so an empty metric gets one made of nulls. */
      series: [
        {},
        ...(names.length
          ? names.map((name) => ({
              label: name,
              stroke: colourOf(name),
              width: 2,
              dash: dashOf(name),
              points: { show: false },
            }))
          : [{ label: '', stroke: 'transparent', points: { show: false } }]),
      ],
    };

    chart = new uPlot(opts, alignedData(), host);
  }

  function alignedData(): uPlot.AlignedData {
    const cols = columns.length ? columns : [new Array(x.length).fill(null)];
    return [x, ...cols] as unknown as uPlot.AlignedData;
  }

  /* What forces a rebuild rather than new numbers in the same shape. Series
     identity, colours and geometry are baked into the options at construction;
     the samples are not. Node visibility is in here because toggling a node
     adds or removes a series. */
  const shape = $derived(
    /* `identity` and `dashed` are baked into the series options at
       construction, exactly like the colours, so they belong in the key that
       decides rebuild-vs-setData. */
    [metric.key, names.join('|'), identity, metric.dashed, theme, width, height, axisWidth].join(
      '~',
    ),
  );
  let builtShape = '';

  /* Annotations are painted in a draw hook, so a change to the list needs a
     REDRAW rather than a rebuild. setData triggers one; a change to the
     annotations alone would otherwise not repaint. */
  $effect(() => {
    void annotations;
    chart?.redraw();
  });

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

  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div
    class="wrap"
    onpointerenter={() => (pointerHere = true)}
    onpointerleave={() => (pointerHere = false)}
  >
    <div class="host" bind:this={host}></div>

    {#if hover && pointerHere && names.length}
      <div
        class="tip"
        class:flip={hover.left > width / 2}
        style:left="{hover.left}px"
        style:top="{hover.top}px"
        aria-hidden="true"
      >
        <div class="when">{stamp(x[hover.idx])}</div>
        {#each nearbyAnnotations(x[hover.idx]) as a (a.kind + a.ts + a.label)}
          <!-- The point of the whole layer: the dip and its candidate
               explanation in one glance, instead of three views and mental
               alignment. -->
          <div class="note" data-kind={a.kind}>{a.label}</div>
        {/each}
        {#each names as name, i (name)}
          {@const v = columns[i]?.[hover.idx]}
          <div class="row">
            <span
              class="swatch"
              class:dashed={metric.dashed === name}
              style:background={colourOf(name)}
            ></span>
            <span class="who">{name}</span>
            <span class="val num">{v == null ? '—' : fmt(v)}</span>
          </div>
        {/each}
      </div>
    {/if}
  </div>
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

  /* The key has to show the DASH, or the tooltip lists two identically
     coloured rows for two lines the reader is meant to tell apart. Gaps rather
     than a lighter tint: a paler swatch reads as a second colour, which is the
     thing the dash exists to avoid. `currentColor` would not work here — the
     colour arrives as an inline background, so the gradient re-uses it via
     `background-image` over that background. */
  .tip .swatch.dashed {
    background-image: repeating-linear-gradient(
      90deg,
      transparent 0 2px,
      var(--panel) 2px 4px
    );
  }

  .tip .who {
    color: var(--ink-muted);
  }

  /* Named, and coloured to match the rule it explains. */
  .tip .note {
    color: var(--ink-2);
    font-size: 10px;
    max-width: 230px;
    white-space: normal;
    margin-top: 2px;
  }

  .tip .note[data-kind='alert'] {
    color: var(--critical);
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
