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
  import { Layout, ZONE_LABEL } from './lib/layout.svelte';
  import type { Zone } from './lib/layout.svelte';
  import { Theme } from './lib/theme.svelte';
  import { LiveFeed } from './lib/live.svelte';
  import { AlertFeed } from './lib/alerts.svelte';
  import { fetchWithTimeout } from './lib/request';
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

  /* Configured endpoints that did not answer.
   *
   * The MIRROR of `unmonitored`: that one catches a server running with
   * nothing collecting it, this catches a collector configured against a
   * server that is not there. A typo'd port, a container renamed, an endpoint
   * retired in fact but not in config.
   *
   * Both are silences, and this is the one that looks healthiest — every part
   * being measured is fine, so the node reads as good while reporting nothing
   * about a router.
   *
   * Only nodes that are UP: on a down node every endpoint is unreachable, and
   * listing each one buries the fact that matters under its consequences. */
  const unreachable = $derived.by<{ node: string; runtime: string; endpoint: string }[]>(() =>
    nodes
      .filter((n) => n.up)
      .flatMap((n) => [
        ...n.runtimes.llama_cpp
          .filter((r) => !r.reachable)
          .map((r) => ({ node: n.node_id, runtime: 'llama.cpp', endpoint: r.endpoint })),
        ...n.runtimes.vllm
          .filter((v) => !v.reachable)
          .map((v) => ({ node: n.node_id, runtime: 'vllm', endpoint: v.server })),
      ]),
  );

  /* G3: configured but long gone.
   *
   * Takes over exactly where the alert gives up. InferenceTargetScrapeFailing
   * resolves after 24h on the assumption the endpoint was retired — reasonable,
   * since an alert that cannot be cleared is one you learn to ignore — but
   * "stops nagging" must not become "is forgotten". At 24h this becomes the
   * record instead.
   *
   * Polled slowly: this changes on the scale of someone tearing down a stack,
   * not on the scale of a scrape. */
  let absent = $state<{ job: string; instance: string; node: string | null; down_for_s: number | null }[]>([]);
  let retiring = $state<string | null>(null);

  async function loadAbsent() {
    try {
      const resp = await fetchWithTimeout('/api/targets/absent');
      if (!resp.ok) return;
      absent = (await resp.json()).targets ?? [];
    } catch {
      // Leave the last known list. A blip here must not make a dead target
      // look resolved.
    }
  }

  async function retire(t: { job: string; instance: string }) {
    retiring = t.instance;
    try {
      const resp = await fetchWithTimeout(
        `/api/targets/absent?job=${encodeURIComponent(t.job)}&instance=${encodeURIComponent(t.instance)}`,
        { method: 'DELETE' },
      );
      if (resp.ok) absent = absent.filter((a) => a.instance !== t.instance);
    } finally {
      retiring = null;
    }
  }

  onMount(() => {
    loadAbsent();
    const timer = setInterval(loadAbsent, 5 * 60_000);
    return () => clearInterval(timer);
  });

  const downFor = (s: number | null) =>
    s === null ? 'never seen up' : s >= 172800 ? `${Math.round(s / 86400)}d` : `${Math.round(s / 3600)}h`;

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

  {#if absent.length}
    <!-- Named as what it IS and what it is not: a target down this long is
         either broken or retired, and nothing observable tells them apart.
         Saying so is more useful than picking one and being wrong. -->
    <p class="notice" data-tone="warning">
      Configured but absent:
      {#each absent as t (t.instance)}
        <span class="num">{t.instance}</span>
        <span class="dim">{t.job}{t.node ? ` on ${t.node}` : ''} · down {downFor(t.down_for_s)}</span>
        {#if t.job === 'vllm'}
          <!-- Removal only, and only for inference. Hardware still exists, so
               being able to delete an environmental target would let someone
               permanently blind the dashboard to a real failure. -->
          <button class="retire" disabled={retiring === t.instance} onclick={() => retire(t)}>
            {retiring === t.instance ? 'removing…' : 'retire'}
          </button>
        {/if}
      {/each}
      <span class="dim">— either broken or deliberately gone; nothing here can tell which.</span>
    </p>
  {/if}

  {#if unreachable.length}
    <!-- Beside `unmonitored` rather than in a panel, for the same reason: it
         reports something ABSENT, and a panel for absent data is a place
         nobody looks. -->
    <p class="notice" data-tone="warning">
      Configured but not answering:
      {#each unreachable as u, i (u.node + u.endpoint)}
        {i > 0 ? ' · ' : ''}<span class="num">{u.endpoint}</span>
        <span class="dim">{u.runtime} on {u.node}</span>
      {/each}
      <span class="dim">— check the port in cluster.yml, or whether the server is running.</span>
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

    <!-- Sections are arrangeable and collapsible; both live in localStorage.
         Node cards and the summary stay put — clustering already orders the
         nodes meaningfully, and the headline belongs at the top.

         THREE ZONES, NOT A GRID. A full-width band above two columns that fill
         INDEPENDENTLY. A CSS grid packs by rows and a row is as tall as its
         tallest item, so a short section beside a tall one strands the space
         beneath it — measured here at 324px between `models` and `processes`,
         enough for two more sections. Independent columns need to be separate
         elements: there is no row for their contents to align to. -->
    <div class="sections" class:dragging={layout.dragId !== null}>
      {@render dropZone('full')}
      <div class="cols">
        {@render dropZone('left')}
        {@render dropZone('right')}
      </div>
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

<!-- One zone renderer for all three, so the drop affordances cannot drift
     apart between them. -->
{#snippet dropZone(z: Zone)}
  {@const ids = layout.inZone(z)}
  <div class="zone" data-zone={z} class:empty={ids.length === 0}>
    {#each ids as id (id)}
      <Section {layout} {id}>
        {#if id === 'models'}
          <ModelsTable {nodes} maxRows={layout.rowsFor(id)} />
        {:else if id === 'processes'}
          <ProcessTable {nodes} maxRows={layout.rowsFor(id)} />
        {:else if id === 'network'}
          <NetworkPanel {nodes} maxRows={layout.rowsFor(id)} />
        {:else if id === 'activity'}
          <SwapTimeline maxRows={layout.rowsFor(id)} />
        {:else if id === 'history'}
          <Trends nodeIds={nodes.map((n) => n.node_id)} themeKey={theme.current} />
        {/if}
      </Section>
    {/each}

    <!-- An empty zone is zero pixels tall, which would make it impossible to
         drag anything INTO — the commonest case being the very first move, out
         of the default single stack. During a drag it becomes a labelled target
         instead. -->
    {#if ids.length === 0}
      <span class="zone-hint">{ZONE_LABEL[z]}</span>
    {/if}

    <!-- Where a release would put it. Absolutely positioned rather than
         inserted into the flow: a placeholder that takes up space would push
         the cards it is measured against, and the measurement would chase
         itself. -->
    {#if layout.drop?.zone === z}
      <div class="drop-line" style:top="{layout.drop.y}px"></div>
    {/if}
  </div>
{/snippet}

<style>
  .shell {
    /* Wide, but not unlimited. 1180px left a third of a 1555px window empty
       and over 2000px of a 34" ultrawide, which is real estate this page can
       use: the history chart gets more resolution per pixel of time, and the
       compact grid fits more nodes per row.
       Still capped, because the tables are the limit rather than the cards —
       past roughly this width the columns of a models or process row drift so
       far apart that tracking one row across them stops being reliable, which
       is the failure a max-width exists to prevent. */
    max-width: 2400px;
    margin: 0 auto;
    padding: 20px 20px 48px;
    display: grid;
    gap: 16px;
    transition: opacity 200ms ease;
  }

  /* More breathing room once the shell is actually using the window. At 20px
     the content runs almost to the bezel on a wide monitor, which reads as
     unfinished rather than spacious. */
  @media (min-width: 1160px) {
    .shell {
      padding-left: 32px;
      padding-right: 32px;
    }
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
    grid-template-columns: minmax(0, 1fr);
    align-items: start;
  }

  @media (min-width: 600px) {
    .node-grid.compact {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }

  @media (min-width: 1160px) {
    .node-grid.compact {
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }
  }

  /* Eight only where a card still clears ~270px — the width at which the
     memory band's legend stays readable. Below that, more columns would buy
     row count at the cost of the one reading the compact card exists to
     show. */
  @media (min-width: 2320px) {
    .node-grid.compact {
      grid-template-columns: repeat(8, minmax(0, 1fr));
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
    grid-template-columns: minmax(0, 1fr);
  }

  @media (min-width: 600px) {
    .node-grid.compact .cluster .nodes {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }

  @media (min-width: 1160px) {
    .node-grid.compact .cluster .nodes {
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }
  }

  @media (min-width: 2320px) {
    .node-grid.compact .cluster .nodes {
      grid-template-columns: repeat(8, minmax(0, 1fr));
    }
  }

  .nodes {
    display: grid;
    gap: 12px;
    /* One column until there's genuinely room for two — the memory band needs
       width to stay readable, and squeezing three narrow bands side by side
       would defeat the comparison it exists to enable. */
    grid-template-columns: minmax(0, 1fr);
  }

  @media (min-width: 900px) {
    .nodes {
      grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
    }
  }

  .retire {
    font-size: 10px;
    letter-spacing: 0.06em;
    padding: 1px 7px;
    margin-left: 4px;
    border-radius: var(--radius);
    border: 1px solid var(--rule);
    color: var(--ink-muted);
    cursor: pointer;
  }

  .retire:hover:not(:disabled) {
    color: var(--ink);
    border-color: var(--ink-muted);
  }

  .retire:disabled {
    opacity: 0.5;
    cursor: default;
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

  /* TWO columns, never more. These are wide tables and a chart; at three
     across the columns collide and the history plot loses the time resolution
     that justifies it. So the count is a constant and the per-section choice
     is just "share a row or take it". */
  .sections {
    display: grid;
    gap: 16px;
    /* Stated rather than left implicit. Without a template the implicit track
       is `auto`, which sizes to CONTENT — so a table that wants more room
       widens the track instead of scrolling inside it. See .cols. */
    grid-template-columns: minmax(0, 1fr);
  }

  /* A zone is a plain vertical stack, and that is the whole point: each fills
     to its own content's height, so a short section can sit under another
     short section regardless of how tall the other column has grown. */
  .zone {
    position: relative;
    display: grid;
    gap: 16px;
    align-content: start;
    /* Same reason as .sections — an implicit `auto` track sizes to content. */
    grid-template-columns: minmax(0, 1fr);
  }

  .cols {
    display: grid;
    gap: 16px;
    /* One column until there is room for two readable tables. Below this the
       two zones stack, which keeps every section full width — a half-width
       table on a laptop is unreadable, and honouring the arrangement there
       would be obeying the letter of it against the point. */
    grid-template-columns: minmax(0, 1fr);
    align-items: start;
  }

  /* minmax(0, 1fr), NEVER a bare 1fr — this is the whole of the dashboard's
     layout shift, and it is not obvious.

     `1fr` is shorthand for `minmax(auto, 1fr)`, and that `auto` minimum means a
     track REFUSES to be narrower than its content's minimum. These columns hold
     wide data tables, so the minimum is large and variable: measured live, the
     two "equal halves" were 813.273px and 769.727px, the left having taken 43px
     from the right simply by containing wider tables.

     The shift follows from the variability. Every time a live value gains a
     digit — "2.9 GiB models" becoming "107.5 GiB models" — the content's
     minimum width changes, the track resizes, and BOTH columns move
     horizontally. That fires on every frame that changes a number's length,
     which on a monitoring dashboard is continuous.

     A 0 minimum makes the tracks exactly equal and immovable. Content that
     genuinely does not fit then scrolls inside its own `.scroll` box, which is
     what that box is for. */
  @media (min-width: 1100px) {
    .cols {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }

  /* Empty zones are invisible and take no space until a drag starts, so the
     page is never decorated with placeholders for arrangements nobody asked
     for. */
  .sections.dragging .zone.empty {
    min-height: 76px;
    border: 1px dashed var(--rule);
    border-radius: var(--radius);
  }

  .zone-hint {
    display: none;
    /* Centred in the empty zone rather than at its top edge: it is a label for
       the whole target, not a heading for a list. */
    position: absolute;
    inset: 0;
    align-items: center;
    justify-content: center;
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-muted);
    pointer-events: none;
  }

  .sections.dragging .zone.empty .zone-hint {
    display: flex;
  }

  /* The destination, drawn where the section will land.
     An accent line rather than a ghost outline of the card: the card being
     carried is already on screen at full size, and a second copy of it reads as
     two sections rather than one being moved. */
  .drop-line {
    position: absolute;
    left: 0;
    right: 0;
    height: 3px;
    border-radius: 2px;
    background: var(--series-1);
    pointer-events: none;
    /* Above the card being carried (z-index 5), deliberately. The card is as
       wide as the column, so whenever the pointer is near the destination the
       card sits directly over it — and the destination is the one thing that
       must stay visible for the whole drag. */
    z-index: 6;
    /* Slides between destinations instead of jumping, which is what makes the
       target legible while the pointer is still moving. */
    transition: top 90ms ease-out;
  }

  /* Caps, so the line reads as an insertion point between two things rather
     than as a rule belonging to the card below it. */
  .drop-line::before,
  .drop-line::after {
    content: '';
    position: absolute;
    top: -3px;
    width: 3px;
    height: 9px;
    border-radius: 2px;
    background: var(--series-1);
  }

  .drop-line::before {
    left: 0;
  }

  .drop-line::after {
    right: 0;
  }
</style>
