<script lang="ts">
  /* One dial: a 240-degree arc, the reading in its middle, the node under it.
   *
   * Shared by System Activity's and Network Activity's gauges (roadmap BA), so
   * a dial means the same thing on both cards. Knows nothing about metrics:
   * the caller passes the fraction, the words and the state.
   */
  import { DIAL_ASPECT, STATE_WORD, arcPath } from '../lib/gauges';
  import type { GaugeState } from '../lib/gauges';

  interface Props {
    /** Width in px; the height follows DIAL_ASPECT, plus the label. */
    size: number;
    /** 0-1 along the sweep. */
    fraction: number;
    /** The node's identity colour, used while the state is ok. */
    colour: string;
    value: string;
    unit: string;
    state?: GaugeState;
    /** [warning, critical] as fractions of the sweep, when there are bands. */
    bands?: [number, number] | null;
    /** The two ends of the scale, printed when the dial is big enough. */
    scale?: [string, string];
    label: string;
    title?: string;
  }
  const {
    size,
    fraction,
    colour,
    value,
    unit,
    state = 'ok',
    bands = null,
    scale,
    label,
    title,
  }: Props = $props();

  const R = 38;
  /* Text inside the drawing is sized in PIXELS and converted to its units, so
     it stays readable on a small dial instead of shrinking with it. */
  const k = $derived(size > 0 ? 100 / size : 1);
  const valuePx = $derived(Math.max(11, Math.min(22, size * 0.19)));
  const unitPx = $derived(Math.max(9, Math.min(11, size * 0.09)));
  const SCALE_PX = 9;

  /* Past a band the whole fill takes the status colour. Node colours are kept
     clear of these hues (scripts/palette_check.py), so it cannot be misread as
     another node; and the word under the value says it too, since status is
     never colour alone. */
  const fill = $derived(
    state === 'critical' ? 'var(--critical)' : state === 'warning' ? 'var(--warning)' : colour,
  );
</script>

<div class="dial" style:--size="{size}px" {title}>
  <svg viewBox="0 0 100 {100 * DIAL_ASPECT}" role="img" aria-label="{label}: {value} {unit}{state !== 'ok' ? `, ${state}` : ''}">
    <path d={arcPath(0, 1, R)} class="track" stroke-width="9" fill="none" />
    {#if bands}
      <path d={arcPath(bands[0], bands[1], 46)} stroke="var(--warning)" stroke-width="2.5" fill="none" />
      <path d={arcPath(bands[1], 1, 46)} stroke="var(--critical)" stroke-width="2.5" fill="none" />
    {/if}
    <path d={arcPath(0, fraction, R)} stroke={fill} stroke-width="9" fill="none" class="fill" />
    <text
      x="50"
      y={50 + valuePx * 0.3 * k}
      text-anchor="middle"
      class="value num {state}"
      font-size={valuePx * k}
    >{value}</text>
    <text
      x="50"
      y={50 + (valuePx * 0.3 + unitPx * 1.5) * k}
      text-anchor="middle"
      class="unit {state}"
      font-size={unitPx * k}
    >{STATE_WORD[state] || unit}</text>
    {#if scale && size >= 110}
      <text x="22" y="78" text-anchor="middle" class="scale" font-size={SCALE_PX * k}>{scale[0]}</text>
      <text x="78" y="78" text-anchor="middle" class="scale" font-size={SCALE_PX * k}>{scale[1]}</text>
    {/if}
  </svg>
  <div class="who"><i style:background={colour}></i>{label}</div>
</div>

<style>
  .dial {
    min-width: 0;
  }

  svg {
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

  /* 14px: DIAL_LABEL_PX in lib/gauges.ts, which the dial size is worked out
     with. */
  .who {
    height: 14px;
    line-height: 14px;
    width: var(--size);
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

  @media (prefers-reduced-motion: no-preference) {
    .fill {
      transition: stroke 200ms ease;
    }
  }
</style>
