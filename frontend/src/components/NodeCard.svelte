<script lang="ts">
  /* One node. The memory band leads, because free capacity is the question
   * this cluster exists to answer; the instrument strip supports it.
   *
   * A down node keeps its card, dimmed, rather than disappearing. A missing
   * card is easy to overlook; an obviously-dead one isn't — and "node down" is
   * the alert this whole system exists to surface.
   */
  import MemoryBand from './MemoryBand.svelte';
  import StatusPill from './StatusPill.svelte';
  import Vitals from './Vitals.svelte';
  import { relativeTime } from '../lib/format';
  import { nodeColorVar } from '../lib/theme';
  import type { NodeSnapshot } from '../lib/types';

  interface Props {
    node: NodeSnapshot;
    /** Categorical slot index, so a node keeps its colour as others come and
     *  go. Colour follows the node, never its position. */
    slot: number;
    /** Reduced to name, status and the memory band.
     *
     * WHAT SURVIVES, AND WHY THOSE. The memory band is the one reading here
     * that is GB10-specific and cannot be inferred from anything else on the
     * page: models, other GPU work and the system all draw from ONE pool, so
     * it is the node's real capacity signal. Status is what tells you whether
     * to look closer. Clock, temperature, power, CPU and pressure are detail
     * you read after deciding to — not scan material.
     *
     * A down node ignores this and keeps its full treatment. Compact is for
     * fitting more healthy nodes on screen; shrinking the one that needs
     * attention would invert the point. */
    compact?: boolean;
  }
  const { node, slot, compact = false }: Props = $props();

  /* Down nodes are never compacted — see `compact` above. */
  const dense = $derived(compact && node.up);

  /* NODE IDENTITY COLOUR — from lib/theme, so the card and the History charts
     cannot drift apart. They did: this file was fixed to eight slots while
     `nodeColor()` stayed on three, and the legend then disagreed with the cards
     from the fourth node on. One definition now. */
  const accent = $derived(nodeColorVar(slot));

  const routerSummary = $derived.by(() => {
    const routers = node.runtimes.llama_cpp;
    if (!routers.length && !node.runtimes.vllm.length) return 'no runtimes';
    const models = routers.reduce((n, r) => n + r.models.length, 0);
    const active = routers.reduce(
      (n, r) => n + r.models.filter((m) => m.state === 'active').length,
      0,
    );
    const parts = [`${routers.length} router${routers.length === 1 ? '' : 's'}`];
    if (models) parts.push(`${active}/${models} loaded`);
    if (node.runtimes.vllm.length) parts.push(`${node.runtimes.vllm.length} vllm`);
    return parts.join(' · ');
  });

  /* Per-node throughput. The headline figure at the top of the page is the
     cluster sum, which answers a different question — "is anything serving?"
     rather than "is THIS box serving?" — and on a multi-node cluster the two
     diverge completely. Summed the same way App does it. */
  const nodeTokensPerSec = $derived(
    (node.runtimes?.llama_cpp ?? []).reduce((a, r) => a + r.tokens_per_sec, 0) +
      (node.runtimes?.vllm ?? []).reduce((a, v) => a + v.tokens_per_sec, 0),
  );
</script>

<!-- FOCUSABLE ONLY WHEN COMPACT, so a sighted keyboard user can reach what
     hover reveals. In full mode nothing is hidden, so the card stays out of the
     tab order rather than adding a stop per node for no reason.
     `group` rather than leaving it roleless: the element genuinely does gather
     several readings under one label, and a focus stop with no role is what the
     a11y rule is warning about.
     Note this is NOT how assistive tech gets the data — see .reveal, which
     stays in the accessibility tree whether or not it is on screen. -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<article
  class="node panel"
  class:down={!node.up}
  class:dense
  style:--accent={accent}
  role={dense ? 'group' : undefined}
  aria-label={dense ? `${node.node_id} details` : undefined}
  tabindex={dense ? 0 : undefined}
>
  <header>
    <h2>
      <span class="mark" aria-hidden="true"></span>
      {node.node_id}
    </h2>
    <div class="meta">
      <StatusPill health={node.health} reasons={node.health_reasons} />
      <!-- The runtime summary goes in compact mode: it is a sentence, and a
           sentence per card is what makes a grid of them unscannable. -->
      {#if !dense}
        <span class="dim sep">{routerSummary}</span>
      {/if}
    </div>
  </header>

  {#if node.up && node.memory}
    <MemoryBand
      totalBytes={node.memory.total_bytes}
      usedBytes={node.memory.used_bytes}
      processes={node.processes}
    />
    {#if dense}
      <!-- COMPACT BY DEFAULT, COMPLETE ON DEMAND.
           Compact keeps only the reading that cannot be inferred elsewhere;
           this is everything it dropped, on hover or focus. Copied from the
           chart tooltip deliberately: absolutely positioned, so it costs no
           layout and cannot reflow the grid it sits in — which is the whole
           point, since a panel that grew on hover would shove every card below
           it down as the pointer crossed the page.
           pointer-events: none, so the pointer never enters it and the card
           beneath keeps the hover. -->
      <div class="reveal">
        <span class="dim">{routerSummary}</span>
        <Vitals
          gpu={node.gpu}
          cpu={node.cpu}
          psi={node.psi}
          memory={node.memory}
          tokensPerSec={nodeTokensPerSec}
        />
      </div>
    {:else}
      <Vitals
        gpu={node.gpu}
        cpu={node.cpu}
        psi={node.psi}
        memory={node.memory}
        tokensPerSec={nodeTokensPerSec}
      />
    {/if}
  {:else}
    <p class="offline">
      No data. Last seen {relativeTime(node.ts)}.
      {#if node.errors.agent}<span class="dim">{node.errors.agent}</span>{/if}
    </p>
  {/if}

  {#if node.up && Object.keys(node.errors).length}
    <p class="errors">
      collector failed: {Object.keys(node.errors).join(', ')}
    </p>
  {/if}
</article>

<style>
  .node {
    /* Positioning context for the compact card's reveal. */
    position: relative;
    padding: 16px 18px;
    display: grid;
    gap: 14px;
    /* The node's identity colour appears once, as a rule — enough to tell
       cards apart at a glance without tinting the whole panel. */
    border-top: 2px solid var(--accent);
  }

  /* What compact mode hides, shown on hover or keyboard focus.
     Anchored to the card's own left/right edges so it reads as belonging to it,
     and lifted above the neighbours it overlaps. */
  .reveal {
    position: absolute;
    top: 100%;
    left: -1px;
    right: -1px;
    z-index: 6;
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px 12px;
    background: var(--panel-raised);
    border: 1px solid var(--rule);
    border-top: none;
    border-radius: 0 0 var(--radius) var(--radius);
    box-shadow: 0 8px 24px rgb(0 0 0 / 0.35);
    pointer-events: none;

    /* VISUALLY hidden, not hidden. `display: none` and `visibility: hidden`
       both drop content out of the accessibility tree, which would mean the
       compact card withheld these readings from a screen reader entirely —
       turning a density preference into an information one. Clipping hides the
       paint and keeps the content announced, so compact is what it claims to
       be: a visual choice. */
    clip-path: inset(100%);
    opacity: 0;
    transition: opacity 120ms ease;
  }

  /* `:focus`, not `:focus-visible`. If focus is on the card, these readings
     are wanted however focus arrived — and :focus-visible deliberately does not
     match programmatic or mouse focus, which would make the reveal depend on
     how you got there. The OUTLINE below keeps :focus-visible, since that is
     exactly the case it exists for: a ring on mouse-click focus is noise. */
  .node.dense:hover .reveal,
  .node.dense:focus .reveal {
    clip-path: none;
    opacity: 1;
  }

  /* The card keeps its own outline when focused, so the reveal is not the only
     signal that focus landed here. */
  .node.dense:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }

  /* Tighter padding and gap, not a smaller type scale. Shrinking the text
     would make a compact card harder to read at the exact moment there are
     more of them to read. */
  .node.dense {
    padding: 10px 12px;
    gap: 8px;
  }

  .node.dense h2 {
    font-size: 15px;
  }

  .node.down {
    opacity: 0.62;
  }

  header {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: 6px 16px;
  }

  h2 {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 19px;
    font-weight: 700;
    letter-spacing: -0.02em;
  }

  .mark {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--accent);
    flex: none;
  }

  .meta {
    display: flex;
    align-items: baseline;
    gap: 12px;
    font-size: 11px;
  }

  .sep {
    border-left: 1px solid var(--rule);
    padding-left: 12px;
  }

  .offline {
    font-size: 12px;
    color: var(--ink-2);
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .errors {
    font-size: 11px;
    color: var(--warning);
  }
</style>
