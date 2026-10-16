<script lang="ts">
  /* Layout presets (roadmap BK): the setup guide's and Appearance's choice of
     a whole arrangement. Choosing one replaces the page's arrangement and
     reloads; on a page the reader has arranged, the first press arms it and
     says so, the second does it, like Reset layout. */
  import { PRESETS, applyPreset, currentPreset, type SketchKind } from '../lib/presets';

  interface Props {
    /** The page differs from what it was set from. */
    customised: boolean;
  }
  const { customised }: Props = $props();

  const current = currentPreset();
  let armed = $state<string | null>(null);
  let timer: ReturnType<typeof setTimeout> | undefined;

  function choose(id: string) {
    if (customised && armed !== id) {
      armed = id;
      clearTimeout(timer);
      timer = setTimeout(() => (armed = null), 4000);
      return;
    }
    applyPreset(id);
    location.reload();
  }

  const FILL: Record<SketchKind, string> = {
    nodes: 'var(--ink)',
    chart: 'var(--ink-muted)',
    gauges: 'var(--ink-muted)',
    table: 'var(--rule)',
  };
</script>

<div class="presets" role="group" aria-label="Layout presets">
  {#each PRESETS as p (p.id)}
    <button
      class="preset"
      class:active={current === p.id}
      aria-pressed={current === p.id}
      onclick={() => choose(p.id)}
    >
      <svg viewBox="-0.2 -0.2 12.4 8.4" aria-hidden="true">
        {#each p.sketch as [x, y, w, h, kind], i (i)}
          <rect {x} {y} width={w} height={h} rx="0.2" class="tile" />
          {#if kind === 'gauges'}
            {@const n = Math.max(1, Math.floor(w / 1.6))}
            {#each Array(n) as _, k (k)}
              <circle cx={x + (w / n) * (k + 0.5)} cy={y + h / 2} r={Math.min(h, w / n) * 0.32} fill="none"
                stroke={FILL.gauges} stroke-width="0.14" />
            {/each}
          {:else if kind === 'chart'}
            <polyline
              points={`${x + 0.3},${y + h * 0.7} ${x + w * 0.3},${y + h * 0.45} ${x + w * 0.55},${y + h * 0.6} ${x + w - 0.3},${y + h * 0.3}`}
              fill="none" stroke={FILL.chart} stroke-width="0.14" />
          {:else}
            <rect x={x + 0.2} y={y + 0.2} width={w - 0.4} height={Math.min(0.35, h - 0.4)} rx="0.1" fill={FILL[kind]} />
          {/if}
        {/each}
      </svg>
      <span class="name">{p.name}{#if current === p.id}<span class="on">&nbsp;· in use</span>{/if}</span>
      <span class="about">{armed === p.id ? 'Press again: replaces your arrangement, keeps the theme.' : p.about}</span>
    </button>
  {/each}
</div>

<style>
  .presets {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 8px;
  }

  .preset {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 8px;
    text-align: left;
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    background: var(--panel-raised);
    color: var(--ink);
    cursor: pointer;
  }

  .preset:hover {
    border-color: var(--ink-muted);
  }

  .preset.active {
    border-color: var(--ink);
  }

  svg {
    width: 100%;
    aspect-ratio: 12.4 / 8.4;
  }

  .tile {
    fill: var(--panel);
    stroke: var(--rule);
    stroke-width: 0.06;
  }

  .name {
    color: var(--ink);
    font-weight: 600;
  }

  .on {
    font-weight: 400;
    color: var(--ink-muted);
  }

  .about {
    color: var(--ink-muted);
    font-size: var(--text-label);
    line-height: 1.45;
  }
</style>
