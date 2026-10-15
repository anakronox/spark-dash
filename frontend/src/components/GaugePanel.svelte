<script lang="ts">
  /* ONE metric, one dial per node: the gauge counterpart of MetricChart.
   *
   * Laid out like a chart -- the same caption, and a box `height` px tall that
   * the card's resize corner sets -- so the card pages and sizes gauges with
   * the machinery it already has for charts. The dials fill the box, wrapping
   * onto a second line when that makes them larger.
   */
  import type { MetricSpec } from '../lib/history';
  import type { NodeSnapshot } from '../lib/types';
  import {
    DIAL_ASPECT,
    STATE_WORD,
    ageLabel,
    arcPath,
    bandsOf,
    ceilingOf,
    dialFit,
    formatReading,
    kindOf,
    liveValue,
    stateOf,
  } from '../lib/gauges';
  import { nodeColorVar } from '../lib/theme';

  interface Props {
    metric: MetricSpec;
    /** The nodes to draw, already filtered, in inventory order. */
    nodes: NodeSnapshot[];
    slots: Map<string, number>;
    height: number;
    /** For the metrics the snapshot lacks: Prometheus's latest values by node,
     *  and how old the oldest of them is. Undefined for a live metric. */
    latest?: { values: Record<string, number>; ageS: number | null };
  }
  const { metric, nodes, slots, height, latest }: Props = $props();

  const LABEL_PX = 14;
  const GAP_PX = 6;
  const R = 38;

  let width = $state(0);
  const ceiling = $derived(ceilingOf(metric));
  const kind = $derived(kindOf(metric));
  // 4px: the box's top padding.
  const fit = $derived(dialFit(nodes.length, width, height - 4, GAP_PX, LABEL_PX));

  const colourOf = (id: string) => {
    const slot = slots.get(id);
    return slot === undefined ? 'var(--rule)' : nodeColorVar(slot);
  };

  const readings = $derived(
    nodes.map((n) => {
      const v = latest ? (n.up ? (latest.values[n.node_id] ?? null) : null) : liveValue(metric.key, n);
      const bands = bandsOf(metric.key, n);
      return { id: n.node_id, v, bands, state: stateOf(v, bands) };
    }),
  );

  /* Text inside the drawing is sized in PIXELS and converted to its units, so
     it stays readable on a small dial instead of shrinking with it. */
  const k = $derived(fit.size > 0 ? 100 / fit.size : 1);
  const valuePx = $derived(Math.max(11, Math.min(22, fit.size * 0.19)));
  const unitPx = $derived(Math.max(9, Math.min(11, fit.size * 0.09)));
  const scalePx = 9;

  /** Idle reads as a word, as the header says it, for the token rates. */
  const idleWord = (v: number | null) =>
    v === 0 && metric.unit === 'tok/s' ? 'idle' : null;
</script>

<figure class="gauge">
  <figcaption>
    <span class="name" class:verbatim={metric.verbatim}>{metric.label}</span>
    {#if latest && latest.ageS != null}
      <!-- Not live: Prometheus's latest sample, and saying how old it is is
           the difference between a reading and a claim. -->
      <span class="age" title="From Prometheus's latest scrape; not in the live feed">
        · {ageLabel(latest.ageS)}
      </span>
    {/if}
  </figcaption>

  <div class="box" style:height="{height}px" bind:clientWidth={width} data-plot>
    {#if !nodes.length}
      <p class="dim none">No nodes reporting.</p>
    {:else if kind === 'tile'}
      <div class="tiles">
        {#each readings as r (r.id)}
          {@const idle = idleWord(r.v)}
          <div class="tile" style:--c={colourOf(r.id)}>
            <b class="num" class:quiet={idle || r.v == null}>{idle ?? formatReading(metric, r.v)}</b>
            <span>{r.id} · {metric.key === 'temperature_headroom' ? '°C left' : metric.unit}</span>
          </div>
        {/each}
      </div>
    {:else}
      <div class="dials" style:--per-line={fit.perLine} style:--size="{fit.size}px">
        {#each readings as r (r.id)}
          {@const f = r.v == null || ceiling == null ? 0 : r.v / ceiling}
          {@const fill =
            r.state === 'critical' ? 'var(--critical)' : r.state === 'warning' ? 'var(--warning)' : colourOf(r.id)}
          <div class="dial" title={r.bands ? `${r.id}: warns above ${r.bands[0]}°C, critical above ${r.bands[1]}°C` : r.id}>
            <svg viewBox="0 0 100 {100 * DIAL_ASPECT}" role="img" aria-label="{metric.label}, {r.id}: {formatReading(metric, r.v)} {metric.unit}{r.state !== 'ok' ? `, ${r.state}` : ''}">
              <path d={arcPath(0, 1, R)} class="track" stroke-width="9" fill="none" />
              {#if r.bands && ceiling}
                <path d={arcPath(r.bands[0] / ceiling, r.bands[1] / ceiling, 46)} stroke="var(--warning)" stroke-width="2.5" fill="none" />
                <path d={arcPath(r.bands[1] / ceiling, 1, 46)} stroke="var(--critical)" stroke-width="2.5" fill="none" />
              {/if}
              <path d={arcPath(0, f, R)} stroke={fill} stroke-width="9" fill="none" class="fill" />
              <text
                x="50"
                y={50 + valuePx * 0.3 * k}
                text-anchor="middle"
                class="value num"
                class:warning={r.state === 'warning'}
                class:critical={r.state === 'critical'}
                font-size={valuePx * k}
              >{formatReading(metric, r.v)}</text>
              <text
                x="50"
                y={50 + (valuePx * 0.3 + unitPx * 1.5) * k}
                text-anchor="middle"
                class="unit"
                class:warning={r.state === 'warning'}
                class:critical={r.state === 'critical'}
                font-size={unitPx * k}
              >{STATE_WORD[r.state] || metric.unit}</text>
              {#if fit.size >= 110}
                <text x="22" y="78" text-anchor="middle" class="scale" font-size={scalePx * k}>0</text>
                <text x="78" y="78" text-anchor="middle" class="scale" font-size={scalePx * k}>{ceiling}</text>
              {/if}
            </svg>
            <div class="who"><i style:background={colourOf(r.id)}></i>{r.id}</div>
          </div>
        {/each}
      </div>
    {/if}
  </div>
</figure>

<style>
  .gauge {
    margin: 0;
    min-width: 0;
  }

  /* The chart's caption, so a gauges copy and a charts copy read as one card
     in two views. */
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

  .name {
    color: var(--ink-2);
  }

  .name.verbatim {
    text-transform: none;
    letter-spacing: 0.02em;
  }

  .age {
    text-transform: none;
    letter-spacing: 0.02em;
  }

  /* Top-aligned, so the dials sit under their caption the way a chart's plot
     does; room the dials do not use falls below them, between rows. */
  .box {
    display: flex;
    align-items: flex-start;
    justify-content: center;
    overflow: hidden;
    padding-top: 4px;
  }

  .dials {
    display: grid;
    grid-template-columns: repeat(var(--per-line), var(--size));
    gap: 6px;
    justify-content: center;
    align-content: center;
  }

  .dial {
    min-width: 0;
  }

  .dial svg {
    display: block;
    width: var(--size);
    height: auto;
  }

  .track {
    stroke: var(--track);
  }

  .value {
    fill: var(--ink);
    font-weight: 600;
  }

  .unit,
  .scale {
    fill: var(--ink-muted);
  }

  .value.warning,
  .unit.warning {
    fill: var(--warning);
  }

  .value.critical,
  .unit.critical {
    fill: var(--critical);
  }

  /* 14px: LABEL_PX in the script, which the dial size is worked out with. */
  .who {
    height: 14px;
    line-height: 14px;
    font-size: var(--text-micro);
    color: var(--ink-muted);
    text-align: center;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .who i {
    display: inline-block;
    width: 7px;
    height: 7px;
    border-radius: 1px;
    margin-right: 4px;
  }

  .tiles {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(96px, 1fr));
    gap: 6px;
    width: 100%;
    align-content: center;
  }

  .tile {
    background: var(--panel-raised);
    border-radius: var(--radius);
    border-left: 3px solid var(--c);
    padding: 8px 10px 6px;
    min-width: 0;
  }

  .tile b {
    display: block;
    font-size: var(--text-title);
    font-weight: 600;
    color: var(--ink);
  }

  .tile b.quiet {
    color: var(--ink-muted);
  }

  .tile span {
    display: block;
    font-size: var(--text-micro);
    color: var(--ink-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .none {
    font-size: var(--text-label);
  }

  @media (prefers-reduced-motion: no-preference) {
    .fill {
      transition: stroke 200ms ease;
    }
  }
</style>
