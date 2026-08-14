<script lang="ts">
  import { onMount } from 'svelte';
  import ConnectionStateView from './components/ConnectionState.svelte';
  import ModelsTable from './components/ModelsTable.svelte';
  import NodeCard from './components/NodeCard.svelte';
  import ProcessTable from './components/ProcessTable.svelte';
  import { LiveFeed } from './lib/live.svelte';
  import { gib, num } from './lib/format';

  const feed = new LiveFeed();

  onMount(() => {
    feed.connect();
    return () => feed.close();
  });

  const nodes = $derived(feed.snapshot?.nodes ?? []);

  const cluster = $derived.by(() => {
    let tokensPerSec = 0;
    let up = 0;
    // The LARGEST single-node free block, not the sum. Summing free memory
    // across nodes implies fungible capacity that doesn't exist — a 70B model
    // has to fit on one machine, so a cluster-wide total would confidently
    // answer "yes" to a question whose real answer is "no".
    let largestFreeBytes = 0;
    let largestFreeNode = '';

    for (const node of nodes) {
      if (node.up) up += 1;
      if (node.up && node.memory) {
        const free = Math.max(0, node.memory.total_bytes - node.memory.used_bytes);
        if (free > largestFreeBytes) {
          largestFreeBytes = free;
          largestFreeNode = node.node_id;
        }
      }
      for (const r of node.runtimes.llama_cpp) tokensPerSec += r.tokens_per_sec;
      for (const v of node.runtimes.vllm) tokensPerSec += v.tokens_per_sec;
    }
    return { tokensPerSec, largestFreeBytes, largestFreeNode, up, total: nodes.length };
  });

  // Theme is a deliberate choice, not an automatic inversion: both modes were
  // stepped separately. Dark leads because this sits beside a terminal.
  let theme = $state<'dark' | 'light'>('dark');
  $effect(() => {
    document.documentElement.dataset.theme = theme;
  });
</script>

<div class="shell" class:stale={feed.stale}>
  <header class="top">
    <div class="brand">
      <h1>spark<span class="dim">-dash</span></h1>
      <span class="dim tag">GB10 cluster</span>
    </div>

    <div class="right">
      <ConnectionStateView
        state={feed.state}
        tick={feed.tick}
        secondsSinceFrame={feed.secondsSinceFrame}
      />
      <button
        class="theme"
        onclick={() => (theme = theme === 'dark' ? 'light' : 'dark')}
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
            {#if cluster.largestFreeNode}
              <span class="dim">on {cluster.largestFreeNode}</span>
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

    <section class="nodes">
      {#each nodes as node, i (node.node_id)}
        <NodeCard {node} slot={i} />
      {/each}
    </section>

    <ModelsTable {nodes} />
    <ProcessTable {nodes} />
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
