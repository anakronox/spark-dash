<script lang="ts">
  import { onMount } from 'svelte';
  import Alerts from './components/Alerts.svelte';
  import AlertHistory from './components/AlertHistory.svelte';
  import Settings from './components/Settings.svelte';
  import Section from './components/Section.svelte';
  import ConnectionStateView from './components/ConnectionState.svelte';
  import ModelsTable from './components/ModelsTable.svelte';
  import NetworkPanel from './components/NetworkPanel.svelte';
  import MemoryBand from './components/MemoryBand.svelte';
  import NodeCard from './components/NodeCard.svelte';
  import ProcessTable from './components/ProcessTable.svelte';
  import SwapTimeline from './components/SwapTimeline.svelte';
  import Trends from './components/Trends.svelte';
  import { Layout } from './lib/layout.svelte';
  import { Theme } from './lib/theme.svelte';
  import { LiveFeed } from './lib/live.svelte';
  import { AlertFeed } from './lib/alerts.svelte';
  import { gib, num } from './lib/format';
  import type { NodeSnapshot, ProcessInfo } from './lib/types';

  const feed = new LiveFeed();
  const alertFeed = new AlertFeed();
  let historyOpen = $state(false);
  let settingsOpen = $state(false);
  const layout = new Layout();
  const theme = new Theme();

  onMount(() => {
    feed.connect();
    alertFeed.start();
    return () => {
      feed.close();
      alertFeed.stop();
    };
  });

  const nodes = $derived(feed.snapshot?.nodes ?? []);

  interface Cluster {
    key: string;
    /** null for a standalone node — used to decide whether to draw a frame. */
    name: string | null;
    nodes: NodeSnapshot[];
    freeBytes: number;
    totalBytes: number;
    usedBytes: number;
    /** Every member's processes, so the pooled band can be broken down by
     *  workload the same way a node's own band is. */
    processes: ProcessInfo[];
    up: number;
  }

  /* Nodes clustered as they're actually deployed. Not every node is in a
     cluster: a standalone node is a cluster of one, which lets everything
     below aggregate uniformly instead of special-casing. */
  const clusters = $derived.by<Cluster[]>(() => {
    const byKey = new Map<string, Cluster>();
    for (const node of nodes) {
      const key = node.cluster ?? node.node_id;
      let g = byKey.get(key);
      if (!g) {
        g = {
          key,
          name: node.cluster,
          nodes: [],
          freeBytes: 0,
          totalBytes: 0,
          usedBytes: 0,
          processes: [],
          up: 0,
        };
        byKey.set(key, g);
      }
      g.nodes.push(node);
      if (node.up) g.up += 1;
      if (node.up && node.memory) {
        g.totalBytes += node.memory.total_bytes;
        g.usedBytes += node.memory.used_bytes;
        g.freeBytes += Math.max(0, node.memory.total_bytes - node.memory.used_bytes);
        /* Members' processes concatenated, so the pooled band splits by the
           same workload classes a single node's does. Only from nodes that are
           UP: a down member contributes neither capacity nor consumption, and
           counting its last-known processes would describe memory nobody
           holds. */
        g.processes.push(...node.processes);
      }
    }
    return [...byKey.values()];
  });

  /* Identity slot per node, counted across the WHOLE page in render order.
   *
   * Was derived from the cluster index plus the member index, which collides:
   * a two-member cluster at index 1 takes slots 1 and 2, and the next cluster
   * — index 2 — takes slot 2 as well. Two nodes, one colour, and the more
   * clusters you have the likelier it gets.
   *
   * A flat running count cannot collide, and it keeps the property that
   * matters: colour follows the node, not its position, because the order it
   * counts is stable for a given cluster layout. */
  const slotOf = $derived.by(() => {
    const m = new Map<string, number>();
    let next = 0;
    for (const c of clusters) for (const n of c.nodes) m.set(n.node_id, next++);
    return m;
  });

  /* Agent builds across the cluster.
   *
   * Silent when uniform, visible when they diverge. A node left on an older
   * agent shows up as a missing feature rather than as a stale node — it has
   * cost real debugging time twice — and with three nodes "did that one
   * actually update?" becomes a routine question. */
  const agentVersions = $derived.by(() => {
    const seen = new Map<string, string[]>();
    for (const n of nodes) {
      if (!n.up) continue;
      const v = n.agent_version || 'unknown';
      seen.set(v, [...(seen.get(v) ?? []), n.node_id]);
    }
    return [...seen.entries()].sort((a, b) => b[1].length - a[1].length);
  });

  const versionsDiverge = $derived(agentVersions.length > 1);

  /* Inference servers running with nothing configured to collect them.
     Grouped by node so three engine processes on one box read as one problem
     rather than three. */
  const unmonitored = $derived.by<[string, string[]][]>(() =>
    nodes
      .filter((n) => n.up && n.unmonitored_runtimes?.length)
      .map((n) => [n.node_id, n.unmonitored_runtimes] as [string, string[]]),
  );

  const cluster = $derived.by(() => {
    let tokensPerSec = 0;
    let up = 0;
    for (const node of nodes) {
      if (node.up) up += 1;
      for (const r of node.runtimes.llama_cpp) tokensPerSec += r.tokens_per_sec;
      for (const v of node.runtimes.vllm) tokensPerSec += v.tokens_per_sec;
    }

    /* The largest block one model could actually occupy: the best any single
       CLUSTER offers. Clustered nodes pool memory, so summing within a
       cluster is real capacity; summing across clusters would describe
       doesn't exist, since a model can't span machines that aren't
       clustered. */
    let largestFreeBytes = 0;
    let largestFreeWhere = '';
    for (const g of clusters) {
      if (g.up && g.freeBytes > largestFreeBytes) {
        largestFreeBytes = g.freeBytes;
        largestFreeWhere = g.name ?? g.nodes[0].node_id;
      }
    }

    return { tokensPerSec, largestFreeBytes, largestFreeWhere, up, total: nodes.length };
  });

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
      <!-- Permanent, unlike the banner below, which renders nothing when all
           is quiet — without this there'd be no way to reach history on a
           healthy day. Understated when there's nothing firing; a counted
           badge when there is. -->
      <button
        class="alerts-trigger"
        data-severity={alertFeed.worst}
        aria-label={alertFeed.alerts.length
          ? `${alertFeed.alerts.length} alerts firing. Open alerts and history.`
          : 'Open alerts and history'}
        onclick={() => (historyOpen = true)}
      >
        <span aria-hidden="true">{alertFeed.alerts.length ? '■' : '▲'}</span>
        <span class="label">alerts</span>
        {#if alertFeed.alerts.length}
          <span class="badge num">{alertFeed.alerts.length}</span>
        {/if}
      </button>

      <!-- The theme picker used to be a <select> here. It moved into settings:
           the header is the most valuable strip on the page, and a control you
           touch twice a year should not hold a permanent seat in it. -->
      <button
        class="settings-trigger"
        aria-label="Open settings"
        onclick={() => (settingsOpen = true)}
      >
        <span aria-hidden="true">⚙</span>
        <span class="label">settings</span>
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
  <Alerts feed={alertFeed} />
  <AlertHistory feed={alertFeed} open={historyOpen} onclose={() => (historyOpen = false)} />
  <Settings {theme} {layout} open={settingsOpen} onclose={() => (settingsOpen = false)} />

  {#if unmonitored.length}
    <!-- Sits with the other cross-cutting notices rather than in a panel: it
         reports something MISSING, and a panel for absent data is a place
         nobody looks. The node otherwise reads as healthy, because everything
         being measured is. -->
    <p class="notice" data-tone="warning">
      Running but not collected:
      {#each unmonitored as [node, runtimes], i (node)}
        {i > 0 ? ' · ' : ''}<span class="num">{runtimes.join(', ')}</span>
        <span class="dim">on {node}</span>
      {/each}
      <span class="dim">— no throughput, queue depth or cache metrics for these.</span>
    </p>
  {/if}

  {#if versionsDiverge}
    <p class="notice" data-tone="warning">
      Nodes are running different agent builds:
      {#each agentVersions as [version, ids], i (version)}
        {i > 0 ? ' · ' : ''}<span class="num">{version}</span>
        <span class="dim">({ids.join(', ')})</span>
      {/each}
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

    <!-- One grid for every node card, so compact mode can flow them into
         columns. Each standalone node is its own "cluster of one" and so has
         its own .nodes wrapper — without promoting those wrappers out of the
         way, each grid would contain exactly one card and the cards would span
         the full width no matter how small they got. -->
    <div class="node-grid" class:compact={layout.compactCards}>
    {#each clusters as cluster (cluster.key)}
      {#if cluster.name}
        <!-- A frame only where clustering is real. Clustered nodes pool memory,
             so their combined free space is a capacity number in its own
             right; standalone nodes get no frame because there's nothing to
             combine. -->
        <section class="cluster">
          <header class="cluster-head">
            <h2>{cluster.name}</h2>
            <span class="dim">{cluster.nodes.length} nodes pooled</span>
          </header>
          <!-- The pooled band, drawn exactly like a node's own. Honest here
               precisely because these nodes are clustered: a model can span
               them, so their combined free space is one number an operator can
               act on. The same bar across UNCLUSTERED nodes would describe
               capacity that does not exist, which is why only a framed cluster
               gets one. -->
          {#if cluster.totalBytes > 0}
            <MemoryBand
              totalBytes={cluster.totalBytes}
              usedBytes={cluster.usedBytes}
              processes={cluster.processes}
            />
          {/if}
          <div class="nodes">
            {#each cluster.nodes as node (node.node_id)}
              <NodeCard {node} slot={slotOf.get(node.node_id) ?? 0} compact={layout.compactCards} />
            {/each}
          </div>
        </section>
      {:else}
        <div class="nodes">
          {#each cluster.nodes as node (node.node_id)}
            <NodeCard {node} slot={slotOf.get(node.node_id) ?? 0} compact={layout.compactCards} />
          {/each}
        </div>
      {/if}
    {/each}
    </div>

    <!-- Sections are reorderable and collapsible; both live in localStorage.
         Node cards and the summary stay put — clustering already orders the
         nodes meaningfully, and the headline belongs at the top. -->
    <div class="sections">
      {#each layout.visible as id, i (id)}
        <Section {layout} index={i} {id}>
          {#if id === 'models'}
            <ModelsTable {nodes} />
          {:else if id === 'processes'}
            <ProcessTable {nodes} />
          {:else if id === 'network'}
            <NetworkPanel {nodes} />
          {:else if id === 'activity'}
            <SwapTimeline />
          {:else if id === 'history'}
            <Trends nodeIds={nodes.map((n) => n.node_id)} themeKey={theme.current} />
          {/if}
        </Section>
      {/each}
    </div>
  {:else if feed.state !== 'offline'}
    <p class="notice">Waiting for the first frame…</p>
  {/if}

  <footer>
    <span class="dim">read-only · never loads or unloads a model</span>
    {#if !layout.isDefault}
      <button class="reset" onclick={() => layout.reset()}>reset layout</button>
    {/if}
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



  /* Matches .alerts-trigger beside it — two buttons that open two panels
     should not look like different kinds of control. */
  .settings-trigger {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    padding: 3px 8px;
    border-radius: var(--radius);
    border: 1px solid var(--rule);
    color: var(--ink-muted);
  }

  .settings-trigger:hover {
    color: var(--ink);
    border-color: var(--ink-muted);
  }

  .settings-trigger .label {
    letter-spacing: 0.08em;
    text-transform: uppercase;
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

  .cluster {
    display: grid;
    gap: 8px;
    padding: 12px 12px 14px;
    border: 1px dashed var(--rule);
    border-radius: var(--radius);
  }

  .cluster-head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: 4px 14px;
    font-size: 11px;
  }

  .cluster-head h2 {
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-2);
  }

  /* Full mode: unchanged, a column of full-width cards. Compact turns this
     into the shared grid the cards flow through. */
  .node-grid {
    display: grid;
    gap: 12px;
  }

  /* POWER-OF-TWO COLUMN COUNTS: 1, 2, 4 — never 3.
     Clusters scale in powers of two, so a 3-wide grid is the one that wastes a
     row: four nodes become 3 + 1 and the second row is mostly empty. Snapping
     to 1/2/4 keeps a power-of-two fleet filling every row exactly.
     Fixed counts rather than auto-fill for the same reason — auto-fill picks
     "as many as fit", which is 3 at this container width. */
  .node-grid.compact {
    grid-template-columns: 1fr;
    align-items: start;
  }

  @media (min-width: 600px) {
    .node-grid.compact {
      grid-template-columns: repeat(2, 1fr);
    }
  }

  /* The shell caps at 1180px, so the grid is never wider than 1140 and four
     columns land at ~276px each. */
  @media (min-width: 1160px) {
    .node-grid.compact {
      grid-template-columns: repeat(4, 1fr);
    }
  }

  /* DIRECT children only. These are the standalone wrappers — one per node,
     since a standalone node is a cluster of one — and they stop generating
     boxes so their cards become items of .node-grid itself. Without the child
     combinator this also caught the wrappers INSIDE a framed cluster, which
     promoted those cards into the cluster's own single-column grid and left
     them full width and stacked. */
  .node-grid.compact > .nodes {
    display: contents;
  }

  /* A framed cluster keeps its frame and spans the full row: the frame means
     "these pool memory", and one covering part of a row would say something
     untrue about which nodes are grouped. */
  .node-grid.compact .cluster {
    grid-column: 1 / -1;
  }

  /* Its members then grid among themselves, inside the frame, on the same
     power-of-two counts — a pooled cluster is exactly where sizes are powers
     of two. Slightly narrower than the outer grid because of the frame's own
     padding. */
  .node-grid.compact .cluster .nodes {
    grid-template-columns: 1fr;
  }

  @media (min-width: 600px) {
    .node-grid.compact .cluster .nodes {
      grid-template-columns: repeat(2, 1fr);
    }
  }

  @media (min-width: 1160px) {
    .node-grid.compact .cluster .nodes {
      grid-template-columns: repeat(4, 1fr);
    }
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
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    padding-top: 4px;
    border-top: 1px solid var(--rule);
  }

  /* Only shown once the order has actually been changed — an always-present
     reset for a layout you never touched is clutter. */
  /* Sits with the connection state and theme picker: page-level controls,
     not part of any panel. Quiet by default — a permanently loud alerts
     button on a healthy dashboard trains you to ignore it. */
  .alerts-trigger {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-muted);
    padding: 3px 8px;
    border-radius: var(--radius);
    border: 1px solid transparent;
  }

  .alerts-trigger:hover {
    color: var(--ink);
    border-color: var(--rule);
  }

  .alerts-trigger[data-severity='critical'],
  .alerts-trigger[data-severity='warning'] {
    color: var(--ink);
    border-color: var(--rule);
  }

  .alerts-trigger .badge {
    padding: 0 5px;
    border-radius: 999px;
    background: var(--rule);
    color: var(--ink);
  }

  @media (max-width: 640px) {
    .alerts-trigger .label {
      /* The glyph and count carry it; the word is the first thing to go. */
      display: none;
    }
  }

  .reset {
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-muted);
    padding: 2px 5px;
    border-radius: var(--radius);
  }

  .reset:hover {
    color: var(--ink);
  }

  .sections {
    display: grid;
    gap: 16px;
  }
</style>
