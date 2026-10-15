<script lang="ts">
  /* One panel of gauges: the gauge counterpart of MetricChart (roadmap BA).
   *
   * Laid out like a chart -- the same caption, and a box `height` px tall that
   * the card's resize corner sets -- so a card pages and sizes gauges with the
   * machinery it already has for charts (`data-plot`, counted by Section).
   * Dials fill the box, wrapping onto a second line when that makes them
   * larger. Tiles are for readings with no ceiling.
   */
  import Dial from './Dial.svelte';
  import { DIAL_GAP_PX, DIAL_LABEL_PX, dialFit } from '../lib/gauges';
  import type { GaugeItem } from '../lib/gauges';

  interface Props {
    title: string;
    /** Keep the title's case: an interface name, not a phrase. */
    verbatim?: boolean;
    /** After the title, muted: how old a reading is, or what it is out of. */
    note?: string;
    noteTitle?: string;
    height: number;
    items: GaugeItem[];
    tiles?: boolean;
    /** Size the dials as though there were this many, so every panel on a
     *  card draws them the same size: the fullest panel's count. */
    sizeFor?: number;
  }
  const { title, verbatim = false, note, noteTitle, height, items, tiles = false, sizeFor = 0 }: Props =
    $props();

  // The box's 4px top padding comes off the height the dials can use.
  const PAD_PX = 4;
  let width = $state(0);
  const fit = $derived.by(() => {
    const n = Math.max(items.length, sizeFor);
    const f = dialFit(n, width, height - PAD_PX, DIAL_GAP_PX, DIAL_LABEL_PX);
    return { size: f.size, perLine: Math.min(f.perLine, items.length) };
  });
</script>

<figure class="gauge">
  <figcaption>
    <span class="name" class:verbatim>{title}</span>
    {#if note}<span class="note" title={noteTitle}>· {note}</span>{/if}
  </figcaption>

  <div class="box" style:height="{height}px" bind:clientWidth={width} data-plot>
    {#if !items.length}
      <p class="dim none">No nodes reporting.</p>
    {:else if tiles}
      <div class="tiles">
        {#each items as it (it.id)}
          <div class="tile" style:--c={it.colour} title={it.title}>
            <b class="num" class:quiet={it.value === 'idle' || it.value === '—'}>{it.value}</b>
            <span>{it.id} · {it.unit}</span>
          </div>
        {/each}
      </div>
    {:else}
      <div class="dials" style:--per-line={fit.perLine} style:--size="{fit.size}px">
        {#each items as it (it.id)}
          <Dial
            size={fit.size}
            fraction={it.fraction}
            colour={it.colour}
            value={it.value}
            unit={it.unit}
            state={it.state}
            bands={it.bands}
            scale={it.scale}
            label={it.id}
            title={it.title}
          />
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

  .name.verbatim,
  .note {
    text-transform: none;
    letter-spacing: 0.02em;
  }

  /* Top-aligned, so the dials sit under their caption the way a chart's plot
     does; room the dials do not use falls below them, between rows. The 4px
     is PAD_PX in the script. */
  .box {
    display: flex;
    align-items: flex-start;
    justify-content: center;
    overflow: hidden;
    padding-top: 4px;
  }

  /* The gap is DIAL_GAP_PX in lib/gauges.ts. */
  .dials {
    display: grid;
    grid-template-columns: repeat(var(--per-line), var(--size));
    gap: 6px;
    justify-content: center;
    align-content: start;
  }

  .tiles {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(96px, 1fr));
    gap: 6px;
    width: 100%;
    align-content: start;
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
</style>
