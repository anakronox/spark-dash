<script lang="ts">
  import { onMount } from 'svelte';
  import Alerts from './components/Alerts.svelte';
  import ConnectionStateView from './components/ConnectionState.svelte';
  import ModelsTable from './components/ModelsTable.svelte';
  import NodeCard from './components/NodeCard.svelte';
  import ProcessTable from './components/ProcessTable.svelte';
  import Trends from './components/Trends.svelte';
  import { LiveFeed } from './lib/live.svelte';
  import { gib, num } from './lib/format';
  import type { NodeSnapshot } from './lib/types';

  const feed = new LiveFeed();

  onMount(() => {
    feed.connect();
    return () => feed.close();
  });

  const nodes = $derived(feed.snapshot?.nodes ?? []);

  interface Group {
    key: string;
    /** null for a standalone node — used to decide whether to draw a frame. */
    name: string | null;
    nodes: NodeSnapshot[];
    freeBytes: number;
    totalBytes: number;
    up: number;
  }

  /* Nodes grouped as they're actually deployed. Not every node is part of a
     cluster: a standalone node is a group of one, which lets everything below
     aggregate uniformly instead of special-casing. */
  const groups = $derived.by<Group[]>(() => {
    const byKey = new Map<string, Group>();
    for (const node of nodes) {
      const key = node.group ?? node.node_id;
      let g = byKey.get(key);
      if (!g) {
        g = { key, name: node.group, nodes: [], freeBytes: 0, totalBytes: 0, up: 0 };
        byKey.set(key, g);
      }
      g.nodes.push(node);
      if (node.up) g.up += 1;
      if (node.up && node.memory) {
        g.totalBytes += node.memory.total_bytes;
        g.freeBytes += Math.max(0, node.memory.total_bytes - node.memory.used_bytes);
      }
    }
    return [...byKey.values()];
  });

  const cluster = $derived.by(() => {
    let tokensPerSec = 0;
    let up = 0;
    for (const node of nodes) {
      if (node.up) up += 1;
      for (const r of node.runtimes.llama_cpp) tokensPerSec += r.tokens_per_sec;
      for (const v of node.runtimes.vllm) tokensPerSec += v.tokens_per_sec;
    }

    /* The largest block one model could actually occupy: the best any single
       GROUP offers. Clustered nodes pool memory, so summing within a group is
       real capacity; summing across groups would describe capacity that
       doesn't exist, since a model can't span machines that aren't
       clustered. */
    let largestFreeBytes = 0;
    let largestFreeWhere = '';
    for (const g of groups) {
      if (g.up && g.freeBytes > largestFreeBytes) {
        largestFreeBytes = g.freeBytes;
        largestFreeWhere = g.name ?? g.nodes[0].node_id;
      }
    }

    return { tokensPerSec, largestFreeBytes, largestFreeWhere, up, total: nodes.length };
  });

  // Theme is a deliberate choice, not an automatic inversion: both modes were
  // stepped separately. Dark leads because this sits beside a terminal.
  //
  // The DOM attribute is set SYNCHRONOUSLY rather than in an $effect. Charts
  // draw to a canvas, so they resolve CSS custom properties to literal colours
  // at build time — and effect ordering isn't guaranteed, so a chart rebuilding
  // before the attribute landed would read the outgoing theme's values. That
  // showed up as near-black gridlines on the light background.
  function applyTheme(next: 'dark' | 'light') {
    document.documentElement.dataset.theme = next;
    theme = next;
  }

  let theme = $state<'dark' | 'light'>('dark');
  applyTheme('dark');
</script>

<div class="shell" class:stale={feed.stale}>
  <header class="top">
    <div class="brand">
      <h1>spark<span class="dim">-dash</span></h1>
      <span class="dim tag">GB10 nodes</span>
    </div>

    <div class="right">
      <ConnectionStateView
        state={feed.state}
        tick={feed.tick}
        secondsSinceFrame={feed.secondsSinceFrame}
      />
      <button
        class="theme"
        onclick={() => applyTheme(theme === 'dark' ? 'light' : 'dark')}
        aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
      >
        {theme === 'dark' ? 'light' : 'dark'}
      </button>
    </div>
  </header>

  {#if feed.state === 'offline' && !feed.snapshot}
    <p class="notice" data-tone="critical">
      Can't reach the dashboard backend. It retries automatically.
    </p>
  {:else if feed.stale}
    <!-- Stale data is called out rather than quietly rendered: numbers that
         look current but aren't are the failure this UI must not have. -->
    <p class="notice" data-tone="warning">
      Showing the last frame received {feed.secondsSinceFrame}s ago. These numbers are not current.
    </p>
  {/if}

  <!-- Above everything: an alert is what you want to see before you start
       reading numbers. Renders nothing when all is quiet. -->
  <Alerts />

  {#if feed.snapshot}
    <section class="summary">
      <!-- One figure carries the hierarchy. Throughput is the only quantity
           here that legitimately sums across the cluster. -->
      <div class="figure">
        <span class="value num">{num(cluster.tokensPerSec, 1)}</span>
        <span class="label">tokens/sec</span>
      </div>

      <dl class="facts">
        <div>
          <dt>largest free block</dt>
          <dd>
            <span class="num">{gib(cluster.largestFreeBytes)}</span> GiB
            {#if cluster.largestFreeWhere}
              <span class="dim">on {cluster.largestFreeWhere}</span>
            {/if}
          </dd>
        </div>
        <div>
          <dt>nodes up</dt>
          <dd class="num" data-alert={cluster.up < cluster.total ? 'yes' : null}>
            {cluster.up}<span class="dim">/{cluster.total}</span>
          </dd>
        </div>
      </dl>
    </section>

    {#each groups as group, gi (group.key)}
      {#if group.name}
        <!-- A frame only where grouping is real. Clustered nodes pool memory,
             so their combined free space is a capacity number in its own
             right; standalone nodes get no frame because there's nothing to
             combine. -->
        <section class="group">
          <header class="group-head">
            <h2>{group.name}</h2>
            <span class="dim">
              {group.nodes.length} nodes pooled · <span class="num">{gib(group.freeBytes)}</span>
              GiB free of {gib(group.totalBytes)}
            </span>
          </header>
          <div class="nodes">
            {#each group.nodes as node, i (node.node_id)}
              <NodeCard {node} slot={gi + i} />
            {/each}
          </div>
        </section>
      {:else}
        <div class="nodes">
          {#each group.nodes as node (node.node_id)}
            <NodeCard {node} slot={gi} />
          {/each}
        </div>
      {/if}
    {/each}

    <ModelsTable {nodes} />
    <ProcessTable {nodes} />
    <!-- History last: the live state is what you came for; this is what you
         scroll to when the live state raises a question. -->
    <Trends nodeIds={nodes.map((n) => n.node_id)} {theme} />
  {:else if feed.state !== 'offline'}
    <p class="notice">Waiting for the first frame…</p>
  {/if}

  <footer>
    <span class="dim">read-only · never loads or unloads a model</span>
  </footer>
</div>

<style>
  .shell {
    max-width: 1180px;
    margin: 0 auto;
    padding: 20px 20px 48px;
    display: grid;
    gap: 16px;
    transition: opacity 200ms ease;
  }

  /* The whole page recedes when data stops arriving — a global, unmissable
     signal that reading these numbers is a mistake. */
  .shell.stale {
    opacity: 0.55;
  }

  .top {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: 10px 16px;
    padding-bottom: 4px;
    border-bottom: 1px solid var(--rule);
  }

  .brand {
    display: flex;
    align-items: baseline;
    gap: 10px;
  }

  h1 {
    font-size: 15px;
    font-weight: 700;
    letter-spacing: 0.02em;
  }

  .tag {
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .right {
    display: flex;
    align-items: center;
    gap: 14px;
  }

  .theme {
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-muted);
    padding: 3px 6px;
    border-radius: var(--radius);
  }

  .theme:hover {
    color: var(--ink);
  }

  .summary {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 12px 36px;
    padding: 2px 0 6px;
  }

  .facts {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 28px;
    margin: 0;
    font-size: 12px;
  }

  .facts div {
    display: flex;
    flex-direction: column;
    gap: 1px;
  }

  .facts dt {
    font-size: 10px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--ink-muted);
  }

  .facts dd {
    margin: 0;
  }

  .facts dd[data-alert='yes'] {
    color: var(--critical);
  }

  .figure {
    display: flex;
    flex-direction: column;
    gap: 1px;
  }

  .value {
    font-size: 30px;
    font-weight: 700;
    letter-spacing: -0.03em;
    line-height: 1.05;
  }

  .label {
    font-size: 10px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--ink-muted);
  }

  .group {
    display: grid;
    gap: 8px;
    padding: 12px 12px 14px;
    border: 1px dashed var(--rule);
    border-radius: var(--radius);
  }

  .group-head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: 4px 14px;
    font-size: 11px;
  }

  .group-head h2 {
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-2);
  }

  .nodes {
    display: grid;
    gap: 12px;
    /* One column until there's genuinely room for two — the memory band needs
       width to stay readable, and squeezing three narrow bands side by side
       would defeat the comparison it exists to enable. */
    grid-template-columns: 1fr;
  }

  @media (min-width: 900px) {
    .nodes {
      grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
    }
  }

  .notice {
    font-size: 12px;
    padding: 9px 12px;
    border-radius: var(--radius);
    background: var(--panel);
    border: 1px solid var(--rule);
    color: var(--ink-2);
  }

  .notice[data-tone='warning'] {
    color: var(--warning);
    border-color: color-mix(in srgb, var(--warning) 40%, var(--rule));
  }

  .notice[data-tone='critical'] {
    color: var(--critical);
    border-color: color-mix(in srgb, var(--critical) 40%, var(--rule));
  }

  footer {
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    padding-top: 4px;
    border-top: 1px solid var(--rule);
  }
</style>
