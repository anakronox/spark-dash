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
  }
  const { node, slot }: Props = $props();

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
</script>

<article class="node panel" class:down={!node.up} style:--accent={accent}>
  <header>
    <h2>
      <span class="mark" aria-hidden="true"></span>
      {node.node_id}
    </h2>
    <div class="meta">
      <StatusPill health={node.health} reasons={node.health_reasons} />
      <span class="dim sep">{routerSummary}</span>
    </div>
  </header>

  {#if node.up && node.memory}
    <MemoryBand
      totalBytes={node.memory.total_bytes}
      usedBytes={node.memory.used_bytes}
      processes={node.processes}
    />
    <Vitals gpu={node.gpu} cpu={node.cpu} psi={node.psi} />
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
