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
  import { DEFAULT_PLOT_PX } from '../lib/layout.svelte';
  import {
    TIME_AXIS_PX,
    crossesDay,
    decadeSplits,
    fitRange,
    mirrorRange,
    niceStep,
    stampOf,
    timeLabels,
  } from '../lib/chart-grid';

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
    annotations?: {
      ts: number;
      kind: string;
      label: string;
      node: string | null;
      /** A maintenance window: drawn as a band from ts to here. */
      end_ts?: number | null;
    }[];
    /** Height of a chart that draws the time axis. One that does not is
     *  shorter by the axis band, so every plot in a grid is the same height. */
    height?: number;
    /** Draw the time axis. A grid passes true for its bottom row only. */
    showTime?: boolean;
    /** Fit the y axis to the data, never narrower than the metric's
     *  `minSpan`. Off, or for a metric with no `minSpan`, the axis is the
     *  fixed one below. */
    fit?: boolean;
    /** A log-like y axis (uPlot's asinh: linear near zero, logarithmic past
     *  1 kb/s, so a mirrored chart keeps its negative side). Network Activity's
     *  group charts only, where one 12 Mb/s spike otherwise flattens every
     *  100 kb/s line beside it. */
    log?: boolean;
    /** The node each column belongs to, for its colour, when the lines are
     *  not named after nodes. A group chart's lines are `sparky in` and
     *  `sparky out`, two lines of one node. */
    identities?: string[];
    /** The node this chart belongs to, named in the caption before the title,
     *  in its colour. Network Activity's interface names repeat across nodes:
     *  `enP7s7` exists on all three boxes. */
    owner?: string;
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
    height = DEFAULT_PLOT_PX,
    showTime = true,
    fit = false,
    log = false,
    identities,
    owner,
    identity,
  }: Props = $props();

  /** The line colour for a named column. */
  const colourOf = (name: string, i: number) =>
    nodeColor(slots.get(identity ?? identities?.[i] ?? name));

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

  /* The date rides on the tooltip when the window crosses a day, because the
     axis then names a day only at midnight. */
  const multiDay = $derived(x.length > 1 && crossesDay(x[0], x[x.length - 1]));
  const stamp = (ts: number) => stampOf(ts, multiDay);

  /** Where the axis tops out on the Full scale, and the ceiling a fitted one
   * stays under.
   *
   * FIXED per metric, not fitted to the window. That was the only scale until
   * AO2, and the argument below is why Fit has a minimum span rather than
   * fitting freely. A fitted axis makes a quiet
   * hour look exactly like a busy one — a 2°C wobble and a 40°C climb draw the
   * same shape — and it would also rescale every chart the moment a node was
   * toggled off, which is movement under the reader's cursor in response to an
   * unrelated action.
   *
   * Throughput has no natural ceiling, so it alone fits to its data. It is
   * flagged in the caption rather than left to look like the others.
   */
  const ceiling = $derived(
    metric.states ? 1 : metric.percent ? 100 : (metric.scaleMax ?? null),
  );

  /** Annotations within half a sample of the hovered instant.
   *
   * Keyed off the sample spacing rather than a fixed number of seconds, so it
   * stays "the event at this point on the chart" at a 60s step and at a 3600s
   * one alike. */
  function nearbyAnnotations(ts: number) {
    if (!annotations.length || x.length < 2) return [];
    const tolerance = Math.abs(x[1] - x[0]) / 2;
    return annotations.filter((a) =>
      a.end_ts != null
        ? ts >= a.ts - tolerance && ts <= a.end_ts + tolerance
        : Math.abs(a.ts - ts) <= tolerance,
    );
  }

  const fmt = (v: number) =>
    metric.states
      ? // "up" / "down", never "1.0". The number is an encoding, not a reading:
        // nobody asks how many a port is.
        (metric.states[Math.round(v)] ?? String(v))
      : metric.si
        ? // A mirrored chart's out side is negative only to sit below the line.
          bitRate(metric.mirror ? Math.abs(v) : v)
        : `${Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(1)}${metric.unit}`;

  /* 11px, up from 10. These are eight small charts rather than one large one,
     so their axes are the densest type on the page and were also the smallest.
     Monospace, so the digits stay in tabular columns down the axis. */
  const AXIS_FONT = '11px ui-monospace, monospace';

  /** Padding above the plot, and below it when no time axis is drawn. */
  const PLOT_PAD = 8;

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
    // Two ticks, two words. A state axis labelled 0.0 / 0.5 / 1.0 asks the
    // reader to remember an encoding, and offers a midpoint that cannot occur.
    if (metric.states) return values.map((v) => metric.states![Math.round(v)] ?? '');
    /* SI axes take one divisor for the whole axis, from the top of the scale.
       Scaling each label on its own produces 0 / 500k / 1M / 1.5M / 2M, where
       the reader re-anchors at every gridline to see that the spacing is even.
       The prefix rides on the unit so the axis still says what it measures. */
    /* A LOG AXIS IS THE EXCEPTION: its gridlines are 1k, 10k, 1M apart, and
       one divisor would print 0.001M. Each label takes its own prefix. */
    if (metric.si && log) {
      return values.map((v) => {
        const a = Math.abs(v);
        const { div, prefix } = siScale(a);
        const n = a / div;
        return `${n < 10 && n % 1 ? n.toFixed(1) : n.toFixed(0)}${prefix}${metric.unit}`;
      });
    }
    if (metric.si) {
      const { div, prefix } = siScale(Math.max(...values.map(Math.abs), 1));
      const fine = div > 1 && values.some((v) => v !== 0 && Math.abs(v) / div < 10);
      return values.map(
        (v) => `${((metric.mirror ? Math.abs(v) : v) / div).toFixed(fine ? 1 : 0)}${prefix}${metric.unit}`,
      );
    }
    const d = decimalsFor(values);
    const labels = values.map((v) => v.toFixed(d));
    /* A scale so fine that three decimals still collide. Exponential is ugly
       and it is the honest answer: a duplicated label is an axis that cannot be
       read at all, and the right fix at that point is a better unit for the
       metric — which is why the fault charts count rather than rate. */
    const distinct = new Set(values).size;
    const usable = new Set(labels).size === distinct ? labels : values.map((v) => v.toExponential(1));
    return usable.map((t) => `${t}${metric.unit}`);
  }

  /** How many decimals a tick needs: enough to name the gridline it sits on.
   *
   * TWO WRONG ANSWERS PRECEDED THIS, and both were checked against every axis
   * in the app rather than argued about.
   *
   * The original was a heuristic — "one decimal if anything on the axis is
   * under 10" — which is right for a 0-1 throughput scale and produces nonsense
   * below it: a fault rate of 0.0002 rendered as `0.0` at BOTH ends of its own
   * scale, two identical labels on one axis.
   *
   * The obvious repair, "fewest decimals that tell the ticks apart", is worse.
   * It made a 4.7 tok/s axis read 0 / 1 / 2 / 4 / 5 — every label distinct, and
   * the one reading "4" sitting on the gridline at 3.5. Distinctness is not the
   * requirement. Naming the gridline accurately is, and distinctness follows
   * from it.
   *
   * So the precision comes from the SPACING: resolve to within a quarter of the
   * gap between ticks. That reproduces the old heuristic everywhere it was
   * right (0.0/0.3/0.5/0.8/1.0 on a 0-1 scale, whole numbers on a percentage or
   * a clock) and drops the decimal where it only ever said `.0`.
   */
  function decimalsFor(values: number[]): number {
    // Whole numbers need no decimal at all, whatever the spacing says. A fault
    // count reading 0.0 / 1.0 / 2.0 is accurate and still spends three
    // characters saying the axis counts in halves, which it does not.
    if (values.every((v) => Number.isInteger(v))) return 0;
    const sorted = [...new Set(values)].sort((a, b) => a - b);
    if (sorted.length < 2) return 0;
    const gap = Math.min(...sorted.slice(1).map((v, i) => v - sorted[i]));
    if (!(gap > 0)) return 0;
    return Math.min(3, Math.max(0, Math.ceil(-Math.log10(gap / 2))));
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
    if (metric.states) {
      return Math.ceil(Math.max(...metric.states.map(textWidth))) + AXIS_PAD;
    }
    // The auto-fitted case has no ceiling, so take the tallest sample instead —
    // a throughput axis reaching 1500 needs room for "1500tok/s".
    const peak = Math.max(1, ...columns.flat().filter((v): v is number => v != null && isFinite(v)).map(Math.abs));
    /* The axis runs PAST the peak: headroom, then rounded out to a step. A
       90 kb/s peak draws a 100 kb/s gridline, and sizing the gutter for
       "90kb/s" cut that label to "00kb/s". */
    const headroom = log ? peak * 1.5 : peak * 1.15;
    const top = ceiling ?? Math.ceil(headroom / niceStep(peak / 4)) * niceStep(peak / 4);
    const candidates = [0, top / 4, top / 2, (top * 3) / 4, top];
    // A log axis labels decades, and "100kb/s" is wider than "45Mb/s".
    if (log) candidates.push(...decadeSplits(-top, top));
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
    /* A window is a STRETCH, so it is a band rather than a tick: filled at a
       low alpha so the lines stay legible through it, edged so a short one
       still registers. Muted ink, not a status colour — a state you chose. */
    maintenance: { token: '--ink-muted', dash: [], alpha: 0.4 },
  };
  const BAND_ALPHA = 0.1;

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
      const end = a.end_ts ?? a.ts;
      if (end < lo || a.ts > hi) continue;
      const style = MARK[a.kind] ?? MARK.deploy;
      const colour = cssVar(style.token);
      ctx.lineWidth = 1;
      ctx.setLineDash(style.dash);

      if (a.end_ts != null) {
        const x0 = u.valToPos(Math.max(a.ts, lo), 'x', true);
        const x1 = u.valToPos(Math.min(a.end_ts, hi), 'x', true);
        ctx.fillStyle = colour;
        ctx.globalAlpha = BAND_ALPHA;
        ctx.fillRect(x0, u.bbox.top, Math.max(1, x1 - x0), u.bbox.height);
        ctx.strokeStyle = colour;
        ctx.globalAlpha = style.alpha;
        for (const edge of [a.ts, a.end_ts]) {
          if (edge < lo || edge > hi) continue;
          const px = Math.round(u.valToPos(edge, 'x', true)) + 0.5;
          ctx.beginPath();
          ctx.moveTo(px, u.bbox.top);
          ctx.lineTo(px, u.bbox.top + u.bbox.height);
          ctx.stroke();
        }
        continue;
      }

      ctx.strokeStyle = colour;
      ctx.globalAlpha = style.alpha;
      const px = Math.round(u.valToPos(a.ts, 'x', true)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(px, u.bbox.top);
      ctx.lineTo(px, u.bbox.top + u.bbox.height);
      ctx.stroke();
    }
    ctx.restore();
  }

  /** A mirrored chart's zero line, brighter than the grid: it is the divide
   *  between the two directions. Beneath the series, like the annotations. */
  function drawZero(u: uPlot) {
    if (!metric.mirror) return;
    const y = Math.round(u.valToPos(0, 'y', true)) + 0.5;
    const ctx = u.ctx;
    ctx.save();
    ctx.strokeStyle = cssVar('--ink-muted');
    ctx.globalAlpha = 0.6;
    ctx.lineWidth = devicePixelRatio || 1;
    ctx.beginPath();
    ctx.moveTo(u.bbox.left, y);
    ctx.lineTo(u.bbox.left + u.bbox.width, y);
    ctx.stroke();
    ctx.restore();
  }

  /** "in" and "out" in the plot's right-hand corners, on a chip of the panel
   *  colour so they stay readable over a line and clear of the tick labels. */
  function drawSides(u: uPlot) {
    if (!metric.mirror) return;
    const ctx = u.ctx;
    const r = devicePixelRatio || 1;
    const right = u.bbox.left + u.bbox.width - 4 * r;
    ctx.save();
    ctx.font = `${10 * r}px ui-monospace, monospace`;
    ctx.textAlign = 'right';
    const chip = (text: string, y: number, base: CanvasTextBaseline) => {
      const w = ctx.measureText(text).width;
      ctx.fillStyle = cssVar('--panel');
      ctx.fillRect(right - w - 3 * r, base === 'top' ? y - r : y - 12 * r, w + 6 * r, 13 * r);
      ctx.fillStyle = cssVar('--ink-muted');
      ctx.textBaseline = base;
      ctx.fillText(text, right, y);
    };
    chip('in ↑', u.bbox.top + 2 * r, 'top');
    chip('out ↓', u.bbox.top + u.bbox.height - 2 * r, 'bottom');
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
      /* The same PLOT height on every row. A chart without the time axis gives
         up the band the axis would take, less the padding that replaces it, so
         the rows above the bottom one are shorter and their lines are drawn to
         the same scale. */
      height: showTime ? height : Math.max(1, height - TIME_AXIS_PX + PLOT_PAD),
      /* Explicit above and below. uPlot's automatic padding was 17px on each
         side, measured: that is where a 132px chart's missing plot went. 8px
         is room for half of an 11px tick label at the top and bottom
         gridlines.
         The sides are fixed too. Left automatic, uPlot widened the right side
         only on charts that drew time labels, for the last one's overhang, so
         the bottom row's plots ended short of the rows above. 22px is half of
         "12:45pm" with room to spare. */
      padding: [PLOT_PAD, 22, showTime ? 0 : PLOT_PAD, 0],
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
        drawClear: [drawAnnotations, drawZero],
        draw: [drawSides],
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
        y: metric.states
          ? /* Padded past both states so the line is never drawn ON the plot
               border, where a flat "up" is indistinguishable from the frame
               itself. The gridlines still land on 0 and 1 because the splits
               below say so — this only moves the frame outward. */
            { range: () => [-0.12, 1.12] as [number, number] }
          : metric.mirror
          ? log
            ? /* asinh, not log: log has no zero and no negatives. Linear
                 inside 1 kb/s, logarithmic past it. */
              {
                distr: 4,
                asinh: 1000,
                log: 10,
                range: (_u, min, max) => mirrorRange(min, max).map((v) => v * 1.5) as [number, number],
              }
            : { range: (_u, min, max) => mirrorRange(min, max) }
          : fit && metric.minSpan
          ? /* Fitted to whatever is drawn: uPlot passes the min and max of the
               visible series, so hiding a node refits to the rest. */
            {
              range: (_u, min, max) =>
                fitRange(min, max, { floor: 0, ceil: fixed ?? Infinity, minSpan: metric.minSpan! }),
            }
          : fixed
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
          /* One line, in a band sized for it, and nothing at all on a chart
             that does not draw the time. Shown either way, because the axis
             is what draws the vertical gridlines; it is the labels and the
             band that go. */
          ticks: { stroke: t.grid, size: showTime ? 4 : 0 },
          font: AXIS_FONT,
          size: showTime ? TIME_AXIS_PX : 0,
          gap: 3,
          space: 64,
          values: (u, splits) =>
            showTime
              ? timeLabels(splits, u.scales.x.min ?? splits[0], u.scales.x.max ?? splits[splits.length - 1])
              : splits.map(() => ''),
        },
        {
          stroke: t.axis,
          grid: { stroke: t.grid, width: 1 },
          ticks: { stroke: t.grid },
          font: AXIS_FONT,
          size: axisWidth,
          /* A label every ~26px rather than uPlot's 30, so a taller chart
             gains gridlines instead of only stretching the ones it had. */
          space: 26,
          /* Two splits, not uPlot's automatic five. On a 0-1 scale it would
             offer 0, 0.25, 0.5, 0.75, 1 — three gridlines for values a boolean
             cannot take. */
          ...(metric.states ? { splits: () => [0, 1] } : {}),
          /* Decades only on a mirrored log axis, thinned outward from zero so
             no two labels sit closer than 24px. */
          ...(metric.mirror && log
            ? {
                splits: (u: uPlot, _i: number, min: number, max: number) => {
                  const all = decadeSplits(min, max);
                  const kept = [0];
                  for (const side of [all.filter((v) => v > 0), all.filter((v) => v < 0).reverse()]) {
                    let last = u.valToPos(0, 'y');
                    for (const v of side) {
                      const at = u.valToPos(v, 'y');
                      if (Math.abs(at - last) >= 24) {
                        kept.push(v);
                        last = at;
                      }
                    }
                  }
                  return kept.sort((a, b) => a - b);
                },
                /* uPlot's default filter for a log axis blanks the splits it
                   would not have labelled itself, as null, and a null
                   formatted as "0b/s" on every decade but the top one. These
                   splits are already the ones to label. */
                filter: (_u: uPlot, splits: number[]) => splits,
              }
            : {}),
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
          ? names.map((name, i) => ({
              label: name,
              stroke: colourOf(name, i),
              width: 2,
              dash: dashOf(name),
              points: { show: false },
              /* STEPPED, and `align: 1` specifically: the sample says what the
                 state was AT that instant, and it held until the next scrape
                 said otherwise. Right-continuous is that claim. The default
                 straight interpolation draws a diagonal between the two, which
                 asserts the port spent the interval passing through states it
                 has no way to be in. */
              ...(metric.states
                ? { paths: uPlot.paths.stepped!({ align: 1 }) }
                : {}),
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
    [
      metric.key,
      names.join('|'),
      identity,
      identities?.join('|'),
      log,
      metric.mirror,
      metric.dashed,
      metric.states?.join('|'),
      theme,
      width,
      height,
      showTime,
      fit,
      axisWidth,
    ].join(
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
    {#if owner}
      <span class="owner" style:color={nodeColor(slots.get(owner))}>{owner}</span>
    {/if}
    <span class="name" class:verbatim={metric.verbatim}>{metric.label}</span>
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
        <div class="when">
          {stamp(x[hover.idx])}
          <!-- Named rather than quietly fitted: this chart's height means
               something different from its neighbours', and a reader comparing
               peaks across the grid deserves to know which one uses its own
               reference. In the tooltip rather than the caption, where it took
               a second line on every chart of a grid that is mostly this kind. -->
          {#if !ceiling}<span class="rel">· own scale</span>{/if}
        </div>
        {#each nearbyAnnotations(x[hover.idx]) as a (a.kind + a.ts + a.label)}
          <!-- The point of the whole layer: the dip and its candidate
               explanation in one glance, instead of three views and mental
               alignment. -->
          <div class="note" data-kind={a.kind}>{a.label}</div>
        {/each}
        {#each names as name, i (name)}
          {@const v = columns[i]?.[hover.idx]}
          <div class="row">
            <!-- `background-color`, NOT the `background` shorthand. The
                 shorthand resets `background-image`, and inline style beats the
                 class rule — so the dashed swatch below silently rendered
                 solid, which is a key that describes a line the chart does not
                 draw. -->
            <span
              class="swatch"
              class:dashed={metric.dashed === name}
              style:background-color={colourOf(name, i)}
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

  /* One line, always. A caption that wraps on a narrow chart pushes that
     chart's plot down out of line with its row. */
  figcaption {
    display: flex;
    align-items: baseline;
    gap: 6px;
    white-space: nowrap;
    overflow: hidden;
    padding: 0 0 2px 2px;
    font-size: var(--text-micro);
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-muted);
  }

  /* Not bold: the CARD's title is the bold one, and a grid of eight bold
     captions would compete with it rather than sit under it. */
  .name {
    color: var(--ink-2);
  }

  /* An identifier keeps its own case — see MetricSpec.verbatim. The letter
     spacing goes with it: it is there to keep uppercase from setting too
     tight, and applied to a mixed-case device name it just looks loose. */
  .name.verbatim {
    text-transform: none;
    letter-spacing: 0.02em;
  }

  /* The node, before the title. Its own case and spacing: it is a name. */
  .owner {
    text-transform: none;
    letter-spacing: 0.02em;
  }

  .tip .rel {
    color: var(--ink-muted);
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
    font-size: var(--text-label);
    white-space: nowrap;
    box-shadow: 0 2px 8px rgb(0 0 0 / 0.25);
  }

  .tip.flip {
    transform: translate(calc(-100% - 12px), -50%);
  }

  .tip .when {
    color: var(--ink-muted);
    font-size: var(--text-micro);
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
    /* 2px on, 2px off across an 8px swatch: two visible gaps, which is the
       fewest that still reads as a dash rather than as a rendering artifact. */
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
    font-size: var(--text-micro);
    max-width: 230px;
    white-space: normal;
    margin-top: 2px;
  }

  .tip .note[data-kind='alert'] {
    color: var(--critical);
  }

  .tip .note[data-kind='maintenance'] {
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
