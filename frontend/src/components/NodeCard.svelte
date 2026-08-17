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

  const accent = $derived(`var(--series-${(slot % 3) + 1})`);

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

<article
  class="node panel"
  class:down={!node.up}
  class:dense
  style:--accent={accent}
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
    {#if !dense}
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
    padding: 16px 18px;
    display: grid;
    gap: 14px;
    /* The node's identity colour appears once, as a rule — enough to tell
       cards apart at a glance without tinting the whole panel. */
    border-top: 2px solid var(--accent);
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
