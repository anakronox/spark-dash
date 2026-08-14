<script lang="ts">
  /* The unified memory pool, as one physical thing being divided up.
   *
   * This is the page's signature element, and it earns that by being the
   * truest thing about GB10 hardware: there is no separate VRAM, so model
   * weights, image generation and the OS all draw on one ~121GiB pool. The
   * question an operator actually has — "can I load another model?" — is
   * answered by the width of the empty segment, not by a percentage.
   *
   * On a discrete-GPU cluster this visual would be wrong. Here it's the point.
   */
  import { breakdown, gib, ratioPct } from '../lib/format';
  import type { ProcessInfo } from '../lib/types';

  interface Props {
    totalBytes: number;
    usedBytes: number;
    processes: ProcessInfo[];
  }

  const { totalBytes, usedBytes, processes }: Props = $props();

  const parts = $derived(breakdown(totalBytes, usedBytes, processes));

  const segments = $derived(
    [
      { key: 'llm', label: 'models', bytes: parts.llmBytes, color: 'var(--series-1)' },
      { key: 'other', label: 'other gpu', bytes: parts.otherGpuBytes, color: 'var(--series-2)' },
      { key: 'system', label: 'system', bytes: parts.systemBytes, color: 'var(--ink-muted)' },
    ].filter((s) => s.bytes > 0),
  );
</script>

<figure class="band">
  <div
    class="track"
    role="img"
    aria-label={`Memory pool: ${gib(usedBytes)} of ${gib(totalBytes)} GiB used, ` +
      `${gib(parts.freeBytes)} GiB free`}
  >
    {#each segments as seg (seg.key)}
      <div
        class="seg"
        style:width={`${ratioPct(seg.bytes, parts.totalBytes)}%`}
        style:background={seg.color}
        title={`${seg.label}: ${gib(seg.bytes)} GiB`}
      ></div>
    {/each}
  </div>

  <figcaption>
    <span class="headline">
      <strong class="num">{gib(parts.freeBytes)}</strong><span class="unit">GiB free</span>
      <span class="of num dim">of {gib(parts.totalBytes)}</span>
    </span>

    <!-- Legend doubles as the values: a separate key would make you look twice
         to answer "how much is ComfyUI holding". -->
    <span class="legend">
      {#each segments as seg (seg.key)}
        <span class="item">
          <span class="swatch" style:background={seg.color}></span>
          <span class="dim">{seg.label}</span>
          <span class="num">{gib(seg.bytes)}</span>
        </span>
      {/each}
    </span>
  </figcaption>
</figure>

<style>
  .band {
    margin: 0;
    display: grid;
    gap: 8px;
  }

  .track {
    display: flex;
    /* 2px gaps between segments so adjacent fills stay distinguishable
       without relying on the colours alone. */
    gap: 2px;
    height: 14px;
    background: var(--track);
    border-radius: var(--radius);
    overflow: hidden;
  }

  .seg {
    height: 100%;
    /* Widths move every 2s. A short transition makes a change legible as
       movement rather than a jump — reduced-motion is honoured globally. */
    transition: width 400ms cubic-bezier(0.4, 0, 0.2, 1);
    min-width: 2px;
  }

  figcaption {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: 8px 20px;
  }

  .headline {
    display: flex;
    align-items: baseline;
    gap: 5px;
  }

  .headline strong {
    font-size: 22px;
    font-weight: 700;
    letter-spacing: -0.02em;
  }

  .unit {
    font-size: 11px;
    color: var(--ink-2);
  }

  .of {
    font-size: 11px;
  }

  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 14px;
    font-size: 11px;
  }

  .item {
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }

  .swatch {
    width: 8px;
    height: 8px;
    border-radius: 2px;
    flex: none;
  }
</style>
