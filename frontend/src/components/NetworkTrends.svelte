<script lang="ts">
  /* The fabric, over time.
   *
   * Fifteen `sparkdash_network_*` families were collected, scraped and kept for
   * 180 days with nothing plotting any of them. The Network table shows
   * instantaneous rates, so a link that degraded overnight, a port that
   * flapped, or a transfer that saturated a 200Gb link at 03:00 left nothing
   * anyone could look at afterwards.
   *
   * SMALL MULTIPLES, ONE CHART PER INTERFACE, EACH ON ITS OWN AXIS — and the
   * reason is measured rather than aesthetic. Over 24h on this cluster the
   * peaks span SIX ORDERS OF MAGNITUDE: 580 Mb/s on a 10Gb management port
   * against 288 b/s on a 200Gb RoCE link. On one shared linear axis the
   * management port flattens every other line onto zero, and the chart then
   * says the interconnect is idle — a stronger and more wrong claim than
   * drawing nothing at all.
   *
   * Grouping does not rescue it, which was the first idea and is worth
   * recording as rejected on evidence: the spread is WITHIN classes, not
   * between them. 580 Mb/s against 871 kb/s is two ports of the same 10Gb
   * model; 368 kb/s against 288 b/s is two RoCE links on one node. Splitting
   * fabric from management halves the chart count and leaves a 1000x range
   * inside each one.
   *
   * The cost, stated plainly: chart count grows with interfaces rather than
   * staying fixed the way the History grid does — 14 here, filtered to those
   * carrying traffic. At 32 nodes this does not hold and something else is
   * needed. Accepted because the alternative misrepresents the fabric today,
   * and a 32-node install is hypothetical while a flat-lined 200Gb link is not.
   */
  import ColumnMenu from './ColumnMenu.svelte';
  import MetricChart from './MetricChart.svelte';
  import NetworkTable, { NETWORK_COLUMNS } from './NetworkTable.svelte';
  import { RANGES, fetchAnnotations, fetchHistory, snapGrid, toColumnar } from '../lib/history';
  import type { Annotation } from '../lib/history';
  import {
    NETWORK_METRICS,
    PORT_STATE,
    buildGrid,
    buildRows,
    columnName,
    columnNode,
    ports,
  } from '../lib/network-history';
  import { ColumnView } from '../lib/columns.svelte';
  import type { Port } from '../lib/network-history';
  import { linkKey } from '../lib/network-history';
  import { nodeColor } from '../lib/theme';
  import type { NodeSnapshot } from '../lib/types';

  interface Props {
    /** Ordered node ids, so chart colours and grid order match the cards. */
    nodeIds: string[];
    /** The live snapshot, for one fact only: which interfaces are paired with
     *  an RDMA device, and so belong to the fabric.
     *
     * FROM THE LIVE FEED RATHER THAN FROM PROMETHEUS, which is worth stating
     * because the charts themselves come from Prometheus. The agent has always
     * known the pairing — it drives the RDMA table and the paired alert
     * exclusion — but it only started EXPORTING it as a metric label in AC1c,
     * so a cluster whose node stack has not been redeployed has the fact live
     * and not in history. Reading it here means the division works today,
     * everywhere, and the metric label stays what places port charts in time.
     *
     * The pairing is current and the window is not; an interface's fabric role
     * does not change hour to hour, so applying today's answer to a 7d window
     * is sound in a way that applying today's THROUGHPUT would not be. */
    nodes: NodeSnapshot[];
    /** Rows before each division's table pages. Infinity = uncapped. */
    maxRows?: number;
    themeKey: string;
  }
  const { nodeIds, nodes, maxRows = 8, themeKey }: Props = $props();

  /** `linkKey(node, iface)` for every interface with an RDMA device on it.
   *
   * Union of the live pairing and whatever history knows, so a node that has
   * dropped out of the live feed keeps its charts in the right division rather
   * than sliding into Management the moment its agent goes quiet. */
  const fabric = $derived.by(() => {
    const keys = new Set<string>();
    for (const node of nodes) {
      for (const port of node.rdma ?? []) {
        if (port.interface) keys.add(linkKey(node.node_id, port.interface));
      }
    }
    for (const p of rdma) if (p.iface) keys.add(linkKey(p.node, p.iface));
    return keys;
  });

  const MODE_KEY = 'spark-dash.network-mode.v1';
  const QUIET_KEY = 'spark-dash.network-quiet.v1';
  const EVENTS_KEY = 'spark-dash.network-events.v1';

  const readFlag = (key: string, fallback: boolean) => {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? fallback : raw === '1';
    } catch {
      return fallback;
    }
  };
  const writeFlag = (key: string, value: boolean) => {
    try {
      localStorage.setItem(key, value ? '1' : '0');
    } catch {
      // Still applied for this session.
    }
  };

  /* Interfaces that were flat zero for the whole window are hidden by default.
   *
   * FILTERED ON THE DATA, NOT ON THE `monitored` FLAG — and that was a real
   * choice. `monitored` already means "an interface somebody cares about", it
   * is maintained in cluster.yml, and on this cluster it happens to select
   * exactly the links carrying traffic, so reusing it here would have worked
   * and cost nothing. It was rejected because that flag exists to decide what
   * ALERTS on, and a flag serving two purposes is how a flag starts lying: the
   * first time someone silences a noisy port they would also, invisibly, stop
   * being able to chart it.
   *
   * "Carried traffic in this window" needs no maintenance and cannot fall out
   * of date. It also reads correctly at both ends: an idle 200Gb link sits at
   * 288 b/s rather than at zero, so the fabric stays on the page, while three
   * wifi ports at a true zero drop out of it. */
  let includeQuiet = $state(readFlag(QUIET_KEY, false));
  let showEvents = $state(readFlag(EVENTS_KEY, true));

  /* Not persisted, same reasoning as the History panel: scoping to one node is
     a question you ask, not a preference you hold. */
  let activeNodes = $state<string[] | null>(null);

  /* CHARTS OR TABLE, and the default is computed while the reader has no
   * opinion.
   *
   * The grid is the better view of a small cluster and does not scale: chart
   * count grows with the link count, and a fully-populated GB10 contributes six
   * interfaces and four RDMA ports. The table costs the same at any size.
   *
   * So an untouched card adapts — table above `TABLE_ABOVE` links — and the
   * first explicit choice wins permanently. A default that keeps overriding
   * someone is not a default, and a card that silently changes shape when a
   * node is added is worse than one that picked wrong to begin with.
   */
  const TABLE_ABOVE = 12;
  let chosenMode = $state<'charts' | 'table' | null>(readMode());

  function readMode(): 'charts' | 'table' | null {
    try {
      const raw = localStorage.getItem(MODE_KEY);
      return raw === 'charts' || raw === 'table' ? raw : null;
    } catch {
      return null;
    }
  }

  function setMode(next: 'charts' | 'table') {
    chosenMode = next;
    try {
      localStorage.setItem(MODE_KEY, next);
    } catch {
      // Still applied for this session.
    }
  }

  /** Links whose full chart is open above the table.
   *
   * NOT PERSISTED, the same call as the node solo filter: which link you are
   * looking at is a question you are asking, not a preference you hold. */
  let opened = $state<string[]>([]);
  const openSet = $derived(new Set(opened));

  function toggleOpen(key: string) {
    opened = openSet.has(key) ? opened.filter((k) => k !== key) : [...opened, key];
  }

  let rangeKey = $state(RANGES[0].key);
  let error = $state<string | null>(null);
  let loading = $state(false);
  let annotations = $state<Annotation[]>([]);

  let names = $state<string[]>([]);
  let columns = $state<(number | null)[][]>([]);
  let x = $state<number[]>([]);
  /* Kept apart from `names`/`columns` because a port is not an interface: it is
     keyed by device rather than by netdev, and several of them can hang off one
     wire. Packing it into the same name space would need a third slot in every
     key for the benefit of one caller. */
  let rdma = $state<Port[]>([]);

  const range = $derived(RANGES.find((r) => r.key === rangeKey) ?? RANGES[0]);
  const slots = $derived(new Map(nodeIds.map((id, i) => [id, i])));

  /** Nodes that actually returned an interface. */
  const plotted = $derived.by(() => {
    const seen = new Set<string>();
    for (const n of names) seen.add(columnNode(n));
    return [
      ...nodeIds.filter((id) => seen.has(id)),
      ...[...seen].filter((n) => n && !nodeIds.includes(n)).sort(),
    ];
  });

  /** Self-healing, as in History: a selection that no longer names anything
   *  plottable collapses back to "all" rather than blanking the card. */
  const active = $derived.by(() => {
    if (!activeNodes) return null;
    const kept = activeNodes.filter((n) => plotted.includes(n));
    return kept.length ? kept : null;
  });

  const grid = $derived(
    buildGrid(names, columns, nodeIds, active, includeQuiet, rdma, fabric),
  );
  const groups = $derived(grid.groups);
  const total = $derived(groups.reduce((n, g) => n + g.charts.length, 0));

  /* THE TABLE NEVER FILTERS. `includeQuiet` is a chart-grid concern — a flat
     line is a chart-sized hole — and in a table a quiet link is one short row
     that says, in the `why` column, that it is down. Hiding it there would be
     hiding the answer. */
  const divisions = $derived(buildRows(names, columns, nodeIds, active, rdma, fabric));
  const linkCount = $derived(divisions.reduce((n, d) => n + d.rows.length, 0));

  const mode = $derived(chosenMode ?? (linkCount > TABLE_ABOVE ? 'table' : 'charts'));

  /* One menu for the card, so both divisions' tables share a column set and a
     storage key. Two menus would be two controls in two corners of one card —
     the arrangement NetworkPanel already rejected for the same reason. */
  const linkCols = new ColumnView('network-history.links', NETWORK_COLUMNS);

  /** The charts the opened rows asked for, in the grid's own order so opening
   *  two links does not depend on which was clicked first. */
  const openCharts = $derived(
    groups.flatMap((g) => g.charts).filter((c) => openSet.has(c.link.key)),
  );

  /* Up to 4 across, snapping 1 / 2 / 4 — the same powers-of-two reasoning as
     the History grid and the node grid, so the three cards line up when they
     sit side by side.
     ONE COUNT FOR THE WHOLE CARD, sized to its biggest division. Sizing each
     division separately was the first attempt and looked wrong on the page:
     Fabric's eight charts went four across while Management's three went two
     across, so the management ports were drawn at twice the width of the RoCE
     links. Chart width is a claim about importance, and that one was backwards.
     A short division simply leaves the rest of its row empty, which is what a
     grid does. */
  const colsFor = (n: number) => (n >= 4 ? 4 : n >= 2 ? 2 : 1);
  const cols = $derived(colsFor(Math.max(0, ...groups.map((g) => g.charts.length))));
  const colsMd = $derived(Math.min(cols, 2));

  const isActive = (id: string) => active === null || active.includes(id);

  function pickNode(id: string, additive: boolean) {
    if (additive) {
      const current = active ?? [...plotted];
      const next = current.includes(id)
        ? current.filter((n) => n !== id)
        : [...current, id];
      activeNodes = next.length ? next : null;
      if (activeNodes && activeNodes.length === plotted.length) activeNodes = null;
      return;
    }
    const soloed = active?.length === 1 && active[0] === id;
    activeNodes = soloed ? null : [id];
  }

  let inflight: AbortController | null = null;

  async function load() {
    inflight?.abort();
    const controller = new AbortController();
    inflight = controller;
    loading = true;
    error = null;

    try {
      /* FOUR QUERIES FOR THE WHOLE CARD, not one per chart. Each returns every
         interface at once — 14 series here — so a grid of 14 charts costs the
         same four range queries as a grid of one. In parallel, because they are
         independent and serialising them would make the card four times slower
         to appear. */
      const responses = await Promise.all(
        NETWORK_METRICS.map(async (metric) => {
          const resp = await fetchHistory(metric, range.minutes, range.step, controller.signal);
          return { metric, resp };
        }),
      );
      try {
        annotations = await fetchAnnotations(range.minutes, range.step, controller.signal);
      } catch {
        annotations = [];
      }
      if (controller.signal.aborted) return;

      /* ALL FOUR MERGED ONTO ONE x AXIS, deliberately. Each is a separate range
         query whose backend computes its own `end = time.time()`, so the grids
         come back offset by milliseconds — and here that offset would land in
         the SAME chart, with rx and tx null at each other's timestamps, drawing
         two combs instead of two lines. Snapping to the step grid is what makes
         a chart's own two series line up, and what makes the whole grid share a
         crosshair. */
      const tagged = responses.flatMap(({ metric, resp }) =>
        resp.series.map((series) => ({ metric, series })),
      );
      /* The direction is not in a series' labels — it is which QUERY returned
         it — so it is attached here, keyed on the series object itself. Naming
         the columns afterwards from `tagged` would give the same answer today
         and only because `toColumnar` happens to preserve input order; keying
         on identity does not depend on that. */
      const metricOf = new Map(tagged.map((t) => [t.series, t.metric]));
      const columnar = toColumnar(
        tagged.map((t) => t.series),
        (s) => columnName({ metric: metricOf.get(s)!, series: s }),
      );
      const step = parseInt(range.step, 10) || 60;
      const snapped = snapGrid(columnar.x, columnar.columns, step);
      x = snapped.x;

      /* Split AFTER the alignment, not before. The port-state series has to
         share the grid's x axis — the whole point of the chart is reading a
         port dropping against the traffic on the same cable at the same
         instant — so it goes through `toColumnar` and `snapGrid` with
         everything else and is separated out here. */
      const portRows = [];
      const linkNames = [];
      const linkColumns = [];
      for (let i = 0; i < tagged.length; i++) {
        if (tagged[i].metric === PORT_STATE) {
          portRows.push({ labels: tagged[i].series.labels, column: snapped.columns[i] });
        } else {
          linkNames.push(columnar.names[i]);
          linkColumns.push(snapped.columns[i]);
        }
      }
      rdma = ports(portRows);
      names = linkNames;
      columns = linkColumns;
    } catch (err) {
      if ((err as Error).name === 'AbortError') return;
      error = (err as Error).message;
      names = [];
      columns = [];
      rdma = [];
      x = [];
    } finally {
      if (inflight === controller) loading = false;
    }
  }

  $effect(() => {
    void range.key;
    load();
  });

  /* Capped at five minutes for the same reason as History: scaling with the
     range put the 7d refresh 84 minutes out, and this interval is also the only
     thing that retries after a failed load. */
  const MAX_REFRESH_MS = 5 * 60_000;
  $effect(() => {
    const period = Math.min(MAX_REFRESH_MS, Math.max(30_000, (range.minutes * 60_000) / 120));
    const timer = setInterval(load, period);
    return () => clearInterval(timer);
  });
</script>

<section class="panel">
  <header>
    <div class="titles">
      <h2 class="eyebrow">Network history</h2>
      {#if plotted.length > 1}
        <span class="legend" role="group" aria-label="Nodes">
          {#each plotted as id (id)}
            {@const on = isActive(id)}
            <button
              class="item"
              class:off={!on}
              aria-pressed={on}
              title={`Show only ${id}'s links — shift-click to add`}
              onclick={(e) => pickNode(id, e.shiftKey)}
            >
              <span class="swatch" style:background={nodeColor(slots.get(id))}></span>
              {id}
            </button>
          {/each}
          {#if active}
            <button class="clear" onclick={() => (activeNodes = null)}>show all</button>
          {/if}
        </span>
      {/if}
      <!-- The KEY to the two lines in every chart, stated once for the grid.
           Dash rather than a second hue because colour is already carrying the
           node here, and keeping that constant is what lets one box's charts
           read as a group across the grid. -->
      <span class="key" aria-hidden="true">
        <span class="line solid"></span>in
        <span class="line dash"></span>out
      </span>
    </div>

    <div class="controls">
      <!-- Two words rather than an icon pair: this switches what the card IS,
           and a glyph would have to be learned. -->
      <div class="modes" role="group" aria-label="View">
        {#each ['charts', 'table'] as const as m (m)}
          <button
            class="range"
            class:active={mode === m}
            aria-pressed={mode === m}
            title={m === 'table'
              ? 'One row per link — the whole cluster at once'
              : 'One chart per link, on its own axis'}
            onclick={() => setMode(m)}
          >{m}</button>
        {/each}
      </div>

      <!-- Chart mode only. In a table a quiet link is one short row that says
           why it is quiet, so there is nothing to hide and nothing to count. -->
      {#if mode === 'charts' && (grid.quiet || includeQuiet)}
        <button
          class="events"
          class:on={includeQuiet}
          aria-pressed={includeQuiet}
          title="Interfaces with no traffic at all in this window"
          onclick={() => writeFlag(QUIET_KEY, (includeQuiet = !includeQuiet))}
        >{grid.quiet} idle</button>
      {/if}

      <button
        class="events"
        class:on={showEvents}
        aria-pressed={showEvents}
        title="Mark alerts, cold starts and agent deploys on the charts"
        onclick={() => writeFlag(EVENTS_KEY, (showEvents = !showEvents))}
      >events{annotations.length ? ` · ${annotations.length}` : ''}</button>

      <div class="ranges" role="group" aria-label="Time range">
        {#each RANGES as r (r.key)}
          <button
            class="range"
            class:active={r.key === rangeKey}
            aria-pressed={r.key === rangeKey}
            onclick={() => (rangeKey = r.key)}
          >
            {r.label}
          </button>
        {/each}
      </div>
    </div>
  </header>

  {#if error}
    <p class="error">Couldn't load network history: {error}</p>
  {:else if !x.length}
    <p class="empty dim">{loading ? 'Loading…' : 'No data in this range.'}</p>
  {:else if !total}
    <!-- Distinct from "no data": the queries answered, and everything they
         returned was filtered out. Saying which is what tells the reader
         whether to change the range or the filter. -->
    <p class="empty dim">
      {grid.quiet
        ? `No interface carried traffic in this window (${grid.quiet} idle).`
        : 'No interfaces to draw.'}
    </p>
  {:else}
    <div class="divisions" class:loading>
      <!-- OPENED CHARTS FIRST, above every division. A link opened from the
           Management table is still a chart, and filing it back under its own
           heading would put it below a table the reader is looking at — or
           off screen entirely on a long one. -->
      {#if mode === 'table' && openCharts.length}
        <section class="division">
          <h3 class="division-head">
            Open
            <span class="count">{openCharts.length}</span>
            <span class="note dim">click a row again to close it</span>
          </h3>
          <div class="charts" style:--cols={cols} style:--cols-md={colsMd}>
            {#each openCharts as c (c.key)}
              <div class="cell">
                <span class="owner">{c.link.node}</span>
                <MetricChart
                  metric={c.metric}
                  {x}
                  columns={c.columns}
                  names={c.names}
                  {slots}
                  identity={c.link.node}
                  theme={themeKey}
                  syncKey="spark-dash-network"
                  annotations={showEvents ? annotations : []}
                />
              </div>
            {/each}
          </div>
        </section>
      {/if}

      <!-- FABRIC AND MANAGEMENT ARE DIFFERENT QUESTIONS, so they are different
           divisions. A flat grid gives a 200Gb RoCE link and a wifi port the
           same weight, and reading the fabric then means picking its charts out
           by remembering which device names are which.
           The heading is shown even when there is only one division: with a
           single group it still says WHICH one, and "Fabric" over four charts
           is the difference between "this is the interconnect" and "these are
           four of the interfaces". -->
      {#each mode === 'table' ? divisions : groups as g (g.key)}
        {@const n = 'rows' in g ? g.rows.length : g.charts.length}
        <section class="division" data-division={g.key}>
          <h3 class="division-head">
            {g.label}
            <span class="count">{n}</span>
            <span class="note dim">{g.note}</span>
            {#if mode === 'table' && g.key === divisions[0]?.key}
              <ColumnMenu of="Network history" groups={[{ label: 'Links', view: linkCols }]} />
            {/if}
          </h3>
          {#if mode === 'table' && 'rows' in g}
            <NetworkTable
              label={g.label}
              rows={g.rows}
              cols={linkCols}
              {slots}
              {maxRows}
              open={openSet}
              ontoggle={toggleOpen}
            />
          {:else if 'charts' in g}
          <div class="charts" style:--cols={cols} style:--cols-md={colsMd}>
            {#each g.charts as c (c.key)}
              <div class="cell">
                <!-- The node above the interface, because the interface name is
                     the chart's title and the node is what disambiguates it:
                     `enP7s7` exists on all three boxes. -->
                <span class="owner">{c.link.node}</span>
                <MetricChart
                  metric={c.metric}
                  {x}
                  columns={c.columns}
                  names={c.names}
                  {slots}
                  identity={c.link.node}
                  theme={themeKey}
                  syncKey="spark-dash-network"
                  annotations={showEvents ? annotations : []}
                />
              </div>
            {/each}
          </div>
          {/if}
        </section>
      {/each}
    </div>
  {/if}
</section>

<style>
  section {
    padding: 12px 16px 12px;
  }

  header {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: 8px 12px;
    margin-bottom: 8px;
  }

  .titles {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 6px 10px;
    min-width: 0;
  }

  h2 {
    margin: 0;
  }

  .legend,
  .key {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px 8px;
  }

  .item {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 1px 6px 1px 4px;
    border: 1px solid transparent;
    border-radius: 999px;
    background: none;
    color: var(--ink-2);
    font: inherit;
    font-size: 11px;
    cursor: pointer;
  }

  .item:hover {
    border-color: var(--rule);
  }

  .item.off {
    color: var(--ink-muted);
  }

  .item.off .swatch {
    opacity: 0.3;
  }

  .swatch {
    width: 8px;
    height: 8px;
    border-radius: 2px;
    flex: none;
  }

  .key {
    font-size: 10px;
    letter-spacing: 0.06em;
    color: var(--ink-muted);
    text-transform: uppercase;
  }

  /* Drawn from the SAME tokens the plot uses, so the key cannot describe a
     line the chart no longer draws. */
  .line {
    display: inline-block;
    width: 14px;
    height: 0;
    margin-right: 2px;
    border-top: 2px solid var(--ink-muted);
  }

  .line.dash {
    border-top-style: dashed;
  }

  .controls {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .ranges {
    display: inline-flex;
    gap: 2px;
  }

  .range,
  .events,
  .clear {
    padding: 2px 7px;
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    background: none;
    color: var(--ink-muted);
    font: inherit;
    font-size: 11px;
    cursor: pointer;
  }

  .range:hover,
  .events:hover,
  .clear:hover {
    color: var(--ink-2);
  }

  .range.active,
  .events.on {
    color: var(--ink);
    border-color: var(--ink-muted);
  }

  .charts {
    display: grid;
    /* `minmax(0, 1fr)`, never a bare `1fr`: a bare track takes its minimum from
       the content, and a canvas that has not been resized yet reports a width
       that then holds the column open. The card would stop shrinking with the
       window — the exact bug the Models card had. */
    grid-template-columns: repeat(var(--cols), minmax(0, 1fr));
    gap: 10px 14px;
  }

  @media (max-width: 1100px) {
    .charts {
      grid-template-columns: repeat(var(--cols-md), minmax(0, 1fr));
    }
  }

  @media (max-width: 700px) {
    .charts {
      grid-template-columns: minmax(0, 1fr);
    }
  }

  /* Dimmed rather than replaced while reloading: swapping the grid for a
     spinner every refresh would make the card flash on its own timer. */
  .divisions.loading {
    opacity: 0.55;
  }

  .divisions {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  .division-head {
    display: flex;
    align-items: baseline;
    gap: 8px;
    margin: 0 0 6px;
    padding-bottom: 4px;
    /* A rule, not a heavier weight. The card already has a title and the node
       legend under it; a bold subheading would compete with both. A hairline
       divides without adding a third level of emphasis. */
    border-bottom: 1px solid var(--rule);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-2);
  }

  .division-head .count {
    font-weight: 400;
    color: var(--ink-muted);
    font-variant-numeric: tabular-nums;
  }

  /* The definition, not decoration. "Fabric" is a word people use loosely, and
     this one means something exact — an interface with an RDMA device on it —
     so the heading says so rather than leaving the reader to infer it from
     which names ended up where. */
  .division-head .note {
    margin-left: auto;
    font-weight: 400;
    font-size: 10px;
    letter-spacing: 0.02em;
    text-transform: none;
  }

  @media (max-width: 700px) {
    .division-head .note {
      display: none;
    }
  }

  .cell {
    min-width: 0;
  }

  /* Node ids keep their case for the same reason the interface name does: this
     is the id from cluster.yml, and it is what every other surface on the page
     — the cards, the legend, the tables — prints unchanged. */
  .owner {
    display: block;
    padding-left: 2px;
    font-size: 9px;
    letter-spacing: 0.04em;
    color: var(--ink-muted);
  }

  .empty,
  .error {
    margin: 8px 0 4px;
    font-size: 12px;
  }

  .error {
    color: var(--critical);
  }
</style>
