<script lang="ts">
  import { onMount } from 'svelte';
  import Alerts from './components/Alerts.svelte';
  import AlertHistory from './components/AlertHistory.svelte';
  import Settings from './components/Settings.svelte';
  import Section from './components/Section.svelte';
  import NodeGroup from './components/NodeGroup.svelte';
  import ConnectionStateView from './components/ConnectionState.svelte';
  import ModelsTable from './components/ModelsTable.svelte';
  import NetworkPanel from './components/NetworkPanel.svelte';
  import MemoryBand from './components/MemoryBand.svelte';
  import NodeCard from './components/NodeCard.svelte';
  import ProcessTable from './components/ProcessTable.svelte';
  import SwapTimeline from './components/SwapTimeline.svelte';
  import Trends from './components/Trends.svelte';
  import NetworkTrends from './components/NetworkTrends.svelte';
  import ThermalPanel from './components/ThermalPanel.svelte';
  import { Layout, ZONE_LABEL } from './lib/layout.svelte';
  import type { Zone } from './lib/layout.svelte';
  import { nodeSlots } from './lib/theme';
  import { Theme } from './lib/theme.svelte';
  import { LiveFeed } from './lib/live.svelte';
  import { pageFocus } from './lib/focus.svelte';
  import { AlertFeed } from './lib/alerts.svelte';
  import { fetchWithTimeout } from './lib/request';
  import { compact, gib, num } from './lib/format';
  import type { NodeSnapshot, ProcessInfo } from './lib/types';
  import { engines, isEngineJob } from './lib/types';

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
    /* The reader's arrangement, applied over inventory order. Sorting HERE
       rather than in the markup keeps one list: the cards, the handle's "n of
       m" and the drag's own reckoning all read the same sequence. */
    const groups = [...byKey.values()];
    const seq = layout.orderGroups(groups.map((g) => g.key));
    return groups.slice().sort((a, b) => seq.indexOf(a.key) - seq.indexOf(b.key));
  });

  /* Identity slot per node: its position in the INVENTORY, which is the order
   * of `cluster.yml`. So a node's colour is its line in that file.
   *
   * Was derived from the cluster index plus the member index, which collides:
   * a two-member cluster at index 1 takes slots 1 and 2, and the next cluster
   * — index 2 — takes slot 2 as well. Two nodes, one colour.
   *
   * Then it was a flat running count over the GROUPED list, which cannot
   * collide but is only stable "for a given cluster layout" — and grouping
   * pulls a cluster's members together, so a node listed between two members
   * of one cluster is shifted by nodes that did not move. Adding hardware
   * changes the layout, which is exactly when you least want every chart to
   * repaint.
   *
   * Counting the ungrouped list makes the invariant one a human can hold:
   * APPEND to cluster.yml and no existing node changes colour, whatever the
   * grouping does. The cost is that colours are no longer visually sequential
   * once grouping reorders the cards, which is the right trade — colour here
   * is identity, not rank. */
  const slotOf = $derived(nodeSlots(nodes.map((n) => n.node_id)));

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
        ...engines(n.runtimes).flatMap(([runtime, list]) =>
          list
            .filter((v) => !v.reachable)
            .map((v) => ({ node: n.node_id, runtime, endpoint: v.server })),
        ),
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

  /* CLUSTER-RELATIVE ALERTS, keyed by node, for the cards to show.
   *
   * Taken from the alert feed rather than recomputed here, and that matters:
   * the rules compare 15-minute AVERAGES, because a healthy pair swings
   * -117..+110 MHz sample to sample. A card recomputing from the live snapshot
   * would use instantaneous values, disagree with the alert constantly, and
   * leave a reader unable to tell which to believe.
   *
   * Node health is deliberately untouched. A node clocking 60MHz below its
   * partner is not unhealthy on its own terms, and colouring the card for it
   * would repeat the mistake W avoided — an indicator firing on something
   * nobody can act on. This is a note ON the card, not a change of its state.
   */
  const CLUSTER_ALERTS: Record<string, string> = {
    ClusterNodeClockLagging: 'clock lagging',
    ClusterNodeRunningHot: 'running hot',
  };

  /* Whether the node the page is scoped to still exists. A node that is DOWN
     is still present and still has rows worth showing; this is about one that
     has left the cluster file entirely. */
  const focusedNodePresent = $derived(
    !pageFocus.scoped || nodes.some((n) => n.node_id === pageFocus.node),
  );

  const stragglers = $derived.by(() => {
    const out = new Map<string, string[]>();
    for (const a of alertFeed.alerts) {
      const label = CLUSTER_ALERTS[a.name];
      if (!label || !a.node) continue;
      out.set(a.node, [...(out.get(a.node) ?? []), label]);
    }
    return out;
  });

  const cluster = $derived.by(() => {
    let tokensPerSec = 0;
    /* Prefill, summed separately and NEVER added to the above -- that sum is
       what made the headline read 47,672 tok/s while the model generated 48
       (Y1). It is reported as a state rather than a rate because it behaves
       like one: measured over six hours, non-zero 1% of the time, peaking at
       110,571 tok/s. There is no moment at which watching it as a number tells
       you anything a two-digit decode rate does not tell you better. */
    let prefillPerSec = 0;
    let up = 0;
    for (const node of nodes) {
      if (node.up) up += 1;
      for (const r of node.runtimes.llama_cpp) {
        tokensPerSec += r.generation_tokens_per_sec ?? 0;
        /* Asymmetric on purpose: the router rolls decode up for us but reports
           no prefill of its own, so this comes from its models. */
        for (const m of r.models) prefillPerSec += m.prompt_tokens_per_sec ?? 0;
      }
      for (const [, list] of engines(node.runtimes)) {
        for (const v of list) {
          tokensPerSec += v.generation_tokens_per_sec ?? 0;
          prefillPerSec += v.prompt_tokens_per_sec ?? 0;
        }
      }
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

    return { tokensPerSec, prefillPerSec, largestFreeBytes, largestFreeWhere, up, total: nodes.length };
  });


  /* PAGE CHROME as named class strings -- see `lib/styles.md`. The layout
     ENGINE stays in CSS below, and the rule dividing them is stated there.

     One thing the split fixed on its own: `.label` meant three different
     things in this file -- the figure's caption, the settings button's word,
     the alerts button's word -- with three different rules resolving by
     specificity. Naming them separately is not tidying; it is the
     "two meanings under one class is how a stylesheet starts lying" problem
     that ModelsTable's `.idle` comment already names. */

  const SHELL =
    'mx-auto grid max-w-[2400px] gap-4 px-5 pt-5 pb-12 transition-opacity ' +
    'duration-200 min-[1160px]:px-8';
  /* The whole page recedes when data stops arriving -- a global, unmissable
     signal that reading these numbers is a mistake. */
  const STALE = 'opacity-[0.55]';

  const TOP =
    'flex flex-wrap items-baseline justify-between gap-x-4 gap-y-[10px] ' +
    'pb-1 border-b border-rule';
  const BRAND = 'flex items-baseline gap-[10px]';
  const H1 = 'text-title-sm font-bold tracking-[0.02em]';
  const RIGHT = 'flex items-center gap-[14px]';

  const MUTED = 'text-ink-muted';
  const NUM = 'tabular-nums';

  /* Two buttons that open two panels should not look like different kinds of
     control, so these share everything but their resting border: alerts is
     quiet until something is firing, settings is always outlined. */
  const TRIGGER_BASE =
    'inline-flex items-center gap-[6px] text-label px-2 py-[3px] rounded-sm border';
  const SETTINGS_TRIGGER =
    `${TRIGGER_BASE} border-rule text-ink-muted hover:text-ink hover:border-ink-muted`;
  const SETTINGS_LABEL = 'text-micro tracking-[0.08em] uppercase';

  /* Quiet by default -- a permanently loud alerts button on a healthy
     dashboard trains you to ignore it. */
  const ALERTS_TRIGGER =
    `${TRIGGER_BASE} tracking-[0.08em] uppercase text-ink-muted border-transparent ` +
    'hover:text-ink hover:border-rule';
  const ALERTS_LOUD = 'text-ink border-rule';
  /* 10px and 0.12em, NOT inherited from the button.
     `.label` meant three things in this file, and these two spans resolved by
     specificity across two of them: the standalone `.label` supplied the size
     (10px) while `.alerts-trigger .label` supplied only what it overrode. Both
     labels therefore rendered a pixel SMALLER than the button around them, and
     letting them simply inherit changed two elements the split was supposed to
     leave alone. Caught by the computed-style diff, not by reading.

     The glyph and the count carry the button; the word is the first to go. */
  const ALERTS_LABEL = 'text-micro tracking-[0.12em] uppercase max-[640px]:hidden';
  const BADGE = 'px-[5px] rounded-full bg-rule text-ink tabular-nums';

  const NOTICE = 'text-body px-3 py-[9px] rounded-sm bg-panel border border-rule text-ink-2';
  /* `info` is neutral ink deliberately: being scoped is a state you chose, not
     a problem. Borrowing the warning colour would put a filter in the same
     visual class as a link that went down. */
  const NOTICE_TONE: Record<string, string> = {
    warning:
      'text-warning [border-color:color-mix(in_srgb,var(--warning)_40%,var(--rule))]',
    info: 'text-ink-muted border-rule',
    critical:
      'text-critical [border-color:color-mix(in_srgb,var(--critical)_40%,var(--rule))]',
  };
  const notice = (tone?: string | null) =>
    tone && NOTICE_TONE[tone] ? `${NOTICE} ${NOTICE_TONE[tone]}` : NOTICE;

  const RETIRE =
    'text-micro tracking-[0.06em] px-[7px] py-px ml-1 rounded-sm border border-rule ' +
    'text-ink-muted cursor-pointer hover:not-disabled:text-ink ' +
    'hover:not-disabled:border-ink-muted disabled:opacity-50 disabled:cursor-default';

  const SUMMARY = 'flex flex-wrap items-baseline gap-x-9 gap-y-3 pt-[2px] pb-[6px]';
  const FACTS = 'flex flex-wrap gap-x-7 gap-y-[10px] m-0 text-body';
  const FACT = 'flex flex-col gap-px';
  const DT = 'text-micro tracking-[0.12em] uppercase text-ink-muted';
  const DD = 'm-0 tabular-nums';
  const DD_ALERT = `${DD} text-critical`;

  /* One figure carries the hierarchy: throughput is the only quantity here
     that legitimately sums across the cluster. */
  const FIGURE = 'flex flex-col gap-px';
  const VALUE = 'text-hero font-bold tracking-[-0.03em] leading-[1.05] tabular-nums';
  const CAPTION = 'text-micro tracking-[0.12em] uppercase text-ink-muted';

  const CLUSTER_HEAD =
    'flex flex-wrap items-baseline justify-between gap-x-[14px] gap-y-1 text-label';
  const CLUSTER_H2 = 'text-label font-medium tracking-[0.14em] uppercase text-ink-2';

  const FOOTER =
    'flex items-baseline justify-between gap-3 text-micro tracking-[0.1em] ' +
    'uppercase pt-1 border-t border-rule';
  const RESET =
    'text-micro tracking-[0.1em] uppercase text-ink-muted px-[5px] py-[2px] ' +
    'rounded-sm hover:text-ink';
</script>

<div class="{SHELL} {feed.stale ? STALE : ''}">
  <header class={TOP}>
    <div class={BRAND}>
      <h1 class={H1}>spark<span class={MUTED}>-dash</span></h1>
    </div>

    <div class={RIGHT}>
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
        class="{ALERTS_TRIGGER} {alertFeed.worst === 'critical' || alertFeed.worst === 'warning'
          ? ALERTS_LOUD
          : ''}"
        aria-label={alertFeed.alerts.length
          ? `${alertFeed.alerts.length} alerts firing. Open alerts and history.`
          : 'Open alerts and history'}
        onclick={() => (historyOpen = true)}
      >
        <span aria-hidden="true">{alertFeed.alerts.length ? '■' : '▲'}</span>
        <span class={ALERTS_LABEL}>alerts</span>
        {#if alertFeed.alerts.length}
          <span class={BADGE}>{alertFeed.alerts.length}</span>
        {/if}
      </button>

      <!-- The theme picker used to be a <select> here. It moved into settings:
           the header is the most valuable strip on the page, and a control you
           touch twice a year should not hold a permanent seat in it. -->
      <button
        class={SETTINGS_TRIGGER}
        aria-label="Open settings"
        onclick={() => (settingsOpen = true)}
      >
        <span aria-hidden="true">⚙</span>
        <span class={SETTINGS_LABEL}>settings</span>
      </button>
    </div>
  </header>

  {#if feed.state === 'offline' && !feed.snapshot}
    <p class={notice('critical')}>
      Can't reach the dashboard backend. It retries automatically.
    </p>
  {:else if feed.stale}
    <!-- Stale data is called out rather than quietly rendered: numbers that
         look current but aren't are the failure this UI must not have. -->
    <p class={notice('warning')}>
      Showing the last frame received {feed.secondsSinceFrame}s ago. These numbers are not current.
    </p>
  {/if}

  <!-- Above everything: an alert is what you want to see before you start
       reading numbers. Renders nothing when all is quiet. -->
  <Alerts feed={alertFeed} />
  <AlertHistory feed={alertFeed} open={historyOpen} onclose={() => (historyOpen = false)} />
  <Settings {theme} {layout} open={settingsOpen} onclose={() => (settingsOpen = false)} />

  <!-- SCOPED, AND SAYING SO. Without this a filtered page is indistinguishable
       from a cluster that lost two nodes — every table short, every count low,
       nothing explaining why. The same unrecoverability rule as hidden
       sections and hidden columns: whatever removes things from the page has
       to be visible ON that page, with the way back attached. -->
  {#if pageFocus.scoped}
    <p class={notice(focusedNodePresent ? 'info' : 'warning')}>
      {#if focusedNodePresent}
        Showing <span class={NUM}>{pageFocus.node}</span> only — every table on
        this page is scoped to it.
      {:else}
        <!-- The dead end this avoids: scope to a node, have it removed from
             cluster.yml, and every table goes empty with a banner naming
             something that no longer exists. Same lesson as clamping the pager
             index when the row count moves — the state outlived what it
             referred to, and saying so beats rendering nothing. -->
        Scoped to <span class={NUM}>{pageFocus.node}</span>, which the cluster
        no longer reports — so every table below is empty.
      {/if}
      <button class={RETIRE} onclick={() => pageFocus.clear()}>show all nodes</button>
    </p>
  {/if}

  {#if unmonitored.length}
    <!-- Sits with the other cross-cutting notices rather than in a panel: it
         reports something MISSING, and a panel for absent data is a place
         nobody looks. The node otherwise reads as healthy, because everything
         being measured is. -->
    <p class={notice('warning')}>
      Running but not collected:
      {#each unmonitored as [node, runtimes], i (node)}
        {i > 0 ? ' · ' : ''}<span class={NUM}>{runtimes.join(', ')}</span>
        <span class={MUTED}>on {node}</span>
      {/each}
      <span class={MUTED}>— no throughput, queue depth or cache metrics for these.</span>
    </p>
  {/if}

  {#if absent.length}
    <!-- Named as what it IS and what it is not: a target down this long is
         either broken or retired, and nothing observable tells them apart.
         Saying so is more useful than picking one and being wrong. -->
    <p class={notice('warning')}>
      Configured but absent:
      {#each absent as t (t.instance)}
        <span class={NUM}>{t.instance}</span>
        <span class={MUTED}>{t.job}{t.node ? ` on ${t.node}` : ''} · down {downFor(t.down_for_s)}</span>
        {#if isEngineJob(t.job)}
          <!-- Removal only, and only for inference. Hardware still exists, so
               being able to delete an environmental target would let someone
               permanently blind the dashboard to a real failure. -->
          <button class={RETIRE} disabled={retiring === t.instance} onclick={() => retire(t)}>
            {retiring === t.instance ? 'removing…' : 'retire'}
          </button>
        {/if}
      {/each}
      <span class={MUTED}>— either broken or deliberately gone; nothing here can tell which.</span>
    </p>
  {/if}

  {#if unreachable.length}
    <!-- Beside `unmonitored` rather than in a panel, for the same reason: it
         reports something ABSENT, and a panel for absent data is a place
         nobody looks. -->
    <p class={notice('warning')}>
      Configured but not answering:
      {#each unreachable as u, i (u.node + u.endpoint)}
        {i > 0 ? ' · ' : ''}<span class={NUM}>{u.endpoint}</span>
        <span class={MUTED}>{u.runtime} on {u.node}</span>
      {/each}
      <span class={MUTED}>— check the port in cluster.yml, or whether the server is running.</span>
    </p>
  {/if}

  {#if versionsDiverge}
    <p class={notice('warning')}>
      Nodes are running different agent builds:
      {#each agentVersions as [version, ids], i (version)}
        {i > 0 ? ' · ' : ''}<span class={NUM}>{version}</span>
        <span class={MUTED}>({ids.join(', ')})</span>
      {/each}
    </p>
  {/if}

  {#if feed.snapshot}
    <section class={SUMMARY}>
      <!-- One figure carries the hierarchy. Throughput is the only quantity
           here that legitimately sums across the cluster. -->
      <div class={FIGURE}>
        <span class={VALUE}>{num(cluster.tokensPerSec, 1)}</span>
        <span class={CAPTION}>decode tok/s</span>
      </div>

      <dl class={FACTS}>
        <div class={FACT}>
          <dt class={DT}>largest free block</dt>
          <dd class={DD}>
            <span class={NUM}>{gib(cluster.largestFreeBytes)}</span> GiB
            {#if cluster.largestFreeWhere}
              <span class={MUTED}>on {cluster.largestFreeWhere}</span>
            {/if}
          </dd>
        </div>
        <div class={FACT}>
          <dt class={DT}>nodes up</dt>
          <dd class={cluster.up < cluster.total ? DD_ALERT : DD}>
            {cluster.up}<span class={MUTED}>/{cluster.total}</span>
          </dd>
        </div>
        <!-- PREFILL AS A STATE, not a rate, and LAST in the row.
             A state because that is what it is: non-zero 1% of the time and
             five to six digits when it fires, so a live number here would read
             "0" nearly always and then briefly dwarf the decode figure it sits
             beside — which is the misreading Y1 removed, reintroduced in a
             smaller font. Last in the row because "ingesting 110k" is wider
             than "idle", and at the end of a flex row a widening value has
             nothing after it to push. -->
        <div class={FACT}>
          <dt class={DT}>prefill</dt>
          <dd class={DD}>
            {#if cluster.prefillPerSec > 0}
              ingesting <span class={NUM}>{compact(cluster.prefillPerSec)}</span>
            {:else}
              <span class={MUTED}>idle</span>
            {/if}
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
    {#each clusters as cluster, gi (cluster.key)}
      <NodeGroup
        {layout}
        groupKey={cluster.key}
        label={cluster.name ?? cluster.nodes[0]?.node_id ?? cluster.key}
        position={`${gi + 1} of ${clusters.length}`}
        all={clusters.map((c) => c.key)}
        framed={!!cluster.name}
      >
      {#if cluster.name}
        <!-- A frame only where clustering is real. Clustered nodes pool memory,
             so their combined free space is a capacity number in its own
             right; standalone nodes get no frame because there's nothing to
             combine. -->
        <section class="cluster">
          <header class={CLUSTER_HEAD}>
            <h2 class={CLUSTER_H2}>{cluster.name}</h2>
            <span class={MUTED}>{cluster.nodes.length} nodes pooled</span>
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
              <NodeCard
                {node}
                slot={slotOf.get(node.node_id) ?? 0}
                compact={layout.compactCards}
                lagging={stragglers.get(node.node_id) ?? []}
              />
            {/each}
          </div>
        </section>
      {:else}
        <div class="nodes">
          {#each cluster.nodes as node (node.node_id)}
            <NodeCard
              {node}
              slot={slotOf.get(node.node_id) ?? 0}
              compact={layout.compactCards}
              lagging={stragglers.get(node.node_id) ?? []}
            />
          {/each}
        </div>
      {/if}
      </NodeGroup>
    {/each}

    <!-- Where a release would put the card being dragged. Inside the grid and
         absolutely positioned, like the sections' line — a placeholder taking
         up space would push the cards it is measured against. -->
    {#if layout.nodeDrop}
      <div class="drop-line" style:top="{layout.nodeDrop.y}px"></div>
    {/if}
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
    <!-- THE PAGE IS A SEQUENCE OF BANDS. It used to be one full-width band
         above two columns, and that could not express "a full-width card below
         a pair" — every column card rendered under every full-width one, so
         columning a card dropped it to the bottom of the page however its
         order read. The order was right; there was nowhere to draw it.

         A band is one full-width card, or a run of consecutive column-placed
         cards. Both come from the `order` and `placement` already stored, so
         nothing new is persisted and an older saved layout opens correctly. -->
    <div class="sections" class:dragging={layout.dragId !== null}>
      {#each layout.bands as band, i (i)}
        {#if band.kind === 'full'}
          {@render zone('full', i, [band.id], null)}
        {:else}
          <div class="cols" class:packed={layout.bandMode === 'packed'} style:--band-rows={band.rows}>
            {@render zone('left', i, band.left, band.last)}
            {@render zone('right', i, band.right, band.last)}
          </div>
        {/if}
      {/each}
    </div>
  {:else if feed.state !== 'offline'}
    <p class={notice()}>Waiting for the first frame…</p>
  {/if}

  <!-- Only when it has something in it. The footer carries a top border, so
       with its text gone and the reset button absent — which is the usual
       state — it would draw a rule across the bottom of the page with nothing
       underneath it. `justify-end` because the button is now the only thing
       here: under `justify-between` a lone child goes to the LEFT, moving a
       control that has always sat on the right. -->
  {#if !layout.isDefault}
    <footer class="{FOOTER} justify-end">
      <button class={RESET} onclick={() => layout.reset()}>reset layout</button>
    </footer>
  {/if}
</div>

<!-- One zone renderer for every zone in every band, so the drop affordances
     cannot drift apart between them.

     `bandLast` is the anchor for a drop into an EMPTY column: there is no card
     to position against, and without it such a drop would fall to the end of
     the page, which is the bug bands exist to fix showing up in the one case
     with nothing visible to aim at. -->
{#snippet zone(z: Zone, band: number, ids: string[], bandLast: string | null)}
  <div
    class="zone"
    data-zone={z}
    data-band={band}
    data-band-last={bandLast}
    class:empty={ids.length === 0}
  >
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
          <Trends nodeIds={nodes.map((n) => n.node_id)} themeKey={theme.resolved} />
        {:else if id === 'thermal'}
          <ThermalPanel {nodes} maxRows={layout.rowsFor(id)} />
        {:else if id === 'network-history'}
          <NetworkTrends
            nodeIds={nodes.map((n) => n.node_id)}
            {nodes}
            maxRows={layout.rowsFor(id)}
            themeKey={theme.resolved}
          />
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
    {#if layout.drop?.band === band && layout.drop.zone === z && layout.drop.kind === 'line'}
      <div class="drop-line" style:top="{layout.drop.y}px"></div>
    {/if}

    <!-- The pair affordance, and it is a BLOCK rather than a line on purpose.
         This gesture changes the width of two cards, which a 3px rule cannot
         say. Drawn at the exact half the dragged card will occupy, so the
         answer to "what happens if I let go here" is the shape under the
         pointer. The target card keeps the other half and needs no marker of
         its own — the empty half beside a filled one reads as the pair. -->
    {#if layout.drop?.band === band && layout.drop.zone === z && layout.drop.kind === 'pair'}
      <div
        class="pair-target"
        data-side={layout.drop.side}
        style:left="{layout.drop.rect.x}px"
        style:top="{layout.drop.rect.y}px"
        style:width="{layout.drop.rect.w}px"
        style:height="{layout.drop.rect.h}px"
      ></div>
    {/if}
  </div>
{/snippet}

<style>
  /* THE LAYOUT ENGINE — and the reason it is still CSS while the chrome above
     is not.

     Every rule below is one where an ANCESTOR's state decides a DESCENDANT's
     layout, at up to four custom breakpoints each: `.node-grid.compact
     .cluster .nodes` is three levels of context before a single declaration.
     Utilities can express that — an ancestor-selector variant, nesting the
     whole `.node-grid.compact` context inside the child's own class and
     stacking a breakpoint prefix on top — but it inverts the reading order and
     repeats that context once per breakpoint, four times, per element.

     The dividing rule, and it is worth stating because it is the one judgement
     call in this migration: AN ELEMENT WHOSE CLASS IS A SELECTOR HOOK FOR AN
     ANCESTOR-STATE RULE KEEPS ITS STYLING HERE. Those classes have to survive
     in the markup regardless, and splitting one element's styling between a
     class attribute and a rule is worse than either alone.

     The breakpoints (600/900/1100/1160/2320) are also none of Tailwind's, and
     they are load-bearing: each is the width at which a specific thing stops
     being readable, documented at its rule. */



  .cluster {
    display: grid;
    gap: 8px;
    padding: 12px 12px 14px;
    border: 1px dashed var(--rule);
    border-radius: var(--radius);
  }



  /* Full mode: unchanged, a column of full-width cards. Compact turns this
     into the shared grid the cards flow through. */
  .node-grid {
    position: relative;
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



  /* The `display: contents` that used to sit here is gone with the wrapper.
     It existed so a standalone node's `.nodes` box stopped generating a frame
     and its card became an item of `.node-grid` itself. A standalone group
     holds exactly one card — a standalone node is a cluster of one — so a
     wrapper occupying one cell renders identically, and the drag handle needs
     a real box to be positioned against. */



  /* A framed cluster keeps its frame and spans the full row: the frame means
     "these pool memory", and one covering part of a row would say something
     untrue about which nodes are grouped. */
  /* :global because the element is NodeGroup's, not this component's. The
     wrapper is the grid item now that every group carries a drag handle, so
     the span has to move to it — left on `.cluster` it would apply to
     something that is no longer a grid item and silently do nothing. */
  .node-grid.compact > :global([data-group-framed]) {
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
    /* STRETCH, so a band's two columns end on the same line.
       `start` left each column at its content height, which is what made a
       band read as two unrelated stacks that happened to be adjacent rather
       than as one row. Where a column holds several cards the slack is shared
       between them, which is what `stretch` does by default and is the only
       distribution that needs no rule to explain it.
       A full-width band is unaffected: its zone is already exactly as tall as
       its content, so there is no slack to share. */
    align-content: stretch;
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
    /* The two zones take the band's full height rather than their own, which
       is what gives the shorter side slack to share. */
    align-items: stretch;
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
      /* THE BAND IS A GRID OF ROWS, and this is the line that makes it one.
         Without explicit rows the two columns are independent stacks: they
         end together but nothing inside them lines up, so the second card on
         the left starts beside the middle of the first card on the right.
         `--band-rows` is the longer column's length, set per band. */
      grid-template-rows: repeat(var(--band-rows, 1), auto);
    }

    /* PACKED MODE IS THE ABSENCE OF THE TWO DECLARATIONS ABOVE, which is why it
       costs so little: the columns become independent stacks again, so a tall
       card on the left can sit beside two short ones on the right and nothing
       is stranded.

       It gives up exactly what aligned mode buys — rows that line up across the
       band. `96a00f4` chose alignment knowing that; this offers the same trade
       to the reader instead of deciding it for them.

       `--band-rows` goes unused here rather than being removed. The band still
       computes it, so switching back needs nothing rebuilt.

       `align-content: start` is the line that matters. Without it the zone
       still stretches to the band's height and the last card grows to fill it,
       which would look like alignment while being something else. */
    .cols.packed {
      grid-template-rows: none;
    }

    .cols.packed > .zone {
      grid-row: auto;
      grid-template-rows: none;
      align-content: start;
    }

    /* Each column spans every row of the band and takes its ROW TRACKS from
       it, so left[n] and right[n] share a row and a height. `subgrid` is what
       lets the columns stay separate elements — which the drag targeting
       needs, since it aims at a zone — while still sharing the band's rows.
       The alternative, one flat grid of cards, would have no column to aim
       at. */
    .cols > .zone {
      grid-row: 1 / -1;
      grid-template-rows: subgrid;
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
    font-size: var(--text-micro);
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

  /* Same accent as the drop line, filled rather than drawn, because this
     gesture is about a SIZE and a line has none. Kept faint: it sits on top of
     a card that is still readable underneath, and the card being carried is
     already on screen at full opacity. */
  .pair-target {
    position: absolute;
    border-radius: var(--radius);
    background: color-mix(in srgb, var(--series-1) 14%, transparent);
    border: 1px solid var(--series-1);
    pointer-events: none;
    z-index: 6;
    /* Slides between the two halves instead of jumping, which is what makes
       the two destinations legible while the pointer is still moving —
       the same reasoning as the drop line's `top` transition. */
    transition:
      left 90ms ease-out,
      top 90ms ease-out,
      width 90ms ease-out,
      height 90ms ease-out;
  }

  /* The outer edge is the one that says which column you land in, so it is the
     one that gets weight. */
  .pair-target[data-side='left'] {
    border-left-width: 3px;
  }

  .pair-target[data-side='right'] {
    border-right-width: 3px;
  }
</style>
