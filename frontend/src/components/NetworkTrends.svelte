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
  import Pager from './Pager.svelte';
  import PickMenu from './PickMenu.svelte';
  import RdmaTable, { RDMA_COLUMNS } from './RdmaTable.svelte';
  import { TableView } from '../lib/table.svelte';
  import { DEFAULT_PLOT_PX, instanceKey } from '../lib/layout.svelte';
  import NetworkTable, { NETWORK_COLUMNS } from './NetworkTable.svelte';
  import { RANGES, fetchAnnotations, fetchHistory, snapGrid, toColumnar } from '../lib/history';
  import type { Annotation } from '../lib/history';
  import {
    NETWORK_METRICS,
    PORT_STATE,
    buildGrid,
    buildRows,
    DIVISIONS,
    DEFAULT_DIVISIONS,
    type Division,
    columnName,
    columnNode,
    pairPorts,
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
    /** Plot height in px, dragged from the card's resize corner. One height for
     *  every division: charts that share an x axis and not a height stop being
     *  comparable, which is the whole reason they are a grid. */
    plotHeight?: number;
    /** Chart ROWS per page per division, Infinity for all -- the chart-mode
     *  counterpart of `maxRows`, and applied per division for the same reason
     *  that cap applies per table. */
    plotRows?: number;
    /** Which card this is, when there is more than one: keys the view, the
     *  groups, quiet and events so two copies do not share them -- one on
     *  ports and one on charts is the whole point of a copy. */
    instance?: string;
  }
  const {
    nodeIds,
    nodes,
    maxRows = 8,
    themeKey,
    plotHeight = DEFAULT_PLOT_PX,
    plotRows = Infinity,
    instance = 'network-history',
  }: Props = $props();

  /** `linkKey(node, iface)` for every interface with an RDMA device on it.
   *
   * Union of the live pairing and whatever history knows, so a node that has
   * dropped out of the live feed keeps its charts in the right division rather
   * than sliding into Management the moment its agent goes quiet. */
  /** Per-interface facts with no time series, from the live snapshot.
   *
   * The negotiated speed and the alerting exclusion moved here when the live
   * Network card stopped drawing a second interface table. Both are the agent's
   * own answers and neither is a rate, so reading them live and applying them
   * to a window is sound in a way that reading live THROUGHPUT would not be. */
  const liveFacts = $derived(
    new Map(
      nodes.flatMap((n) =>
        (n.network ?? []).map(
          (i) =>
            [
              linkKey(n.node_id, i.name),
              {
                speedMbps: i.speed_mbps,
                wireless: i.wireless,
                driver: i.driver,
                bus: i.bus,
                /* Defaulted true for a snapshot from an agent that predates the
                   flag: an older agent watches everything, which is what the
                   field means. */
                monitored: i.monitored ?? true,
              },
            ] as const,
        ),
      ),
    ),
  );

  /** The RoCE pairings the live feed knows, flattened. */
  const livePairs = $derived(
    nodes.flatMap((n) =>
      (n.rdma ?? [])
        .filter((p) => p.interface)
        .map((p) => ({ node: n.node_id, device: p.device, iface: p.interface })),
    ),
  );

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

  // svelte-ignore state_referenced_locally -- `instance` is fixed for the life of the
  // component: App keys each card by id, so a different instance is a different mount.
  const MODE_KEY = instanceKey('spark-dash.network-mode.v1', instance);
  // svelte-ignore state_referenced_locally -- `instance` is fixed for the life of the
  // component: App keys each card by id, so a different instance is a different mount.
  const QUIET_KEY = instanceKey('spark-dash.network-quiet.v1', instance);
  // svelte-ignore state_referenced_locally -- `instance` is fixed for the life of the
  // component: App keys each card by id, so a different instance is a different mount.
  const EVENTS_KEY = instanceKey('spark-dash.network-events.v1', instance);

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
  /* DECLARED BEFORE `chosenMode`, and that order is load-bearing. readMode()
     runs inside the $state initialiser below and reads MODES; a `const`
     declared after that line is in its temporal dead zone at that moment,
     the ReferenceError is swallowed by readMode's own try/catch, and every
     card comes back on its automatic view with its stored choice ignored --
     which is exactly what happened, silently, until a copy made it visible. */
  type Mode = 'charts' | 'table' | 'ports';
  const MODES: Mode[] = ['charts', 'table', 'ports'];
  let chosenMode = $state<Mode | null>(readMode());

  function readMode(): Mode | null {
    try {
      const raw = localStorage.getItem(MODE_KEY);
      return MODES.includes(raw as Mode) ? (raw as Mode) : null;
    } catch {
      return null;
    }
  }

  function setMode(next: Mode) {
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
    buildGrid(names, columns, nodeIds, active, includeQuiet, rdma, fabric, liveFacts),
  );
  /* WHICH GROUPS TO SHOW. Card-local like the metric selection on System
     Activity, and for the same reason: it is a choice about what this card
     draws, not about the page. The menu lists all four groups whether or not
     they have interfaces right now, with the count on each, so a group that
     is hidden and NOT empty is visible as a number rather than invisible. */
  // svelte-ignore state_referenced_locally -- `instance` is fixed for the life of the
  // component: App keys each card by id, so a different instance is a different mount.
  const GROUPS_KEY = instanceKey('spark-dash.network-groups.v1', instance);
  function readGroups(): Division[] {
    try {
      const saved = JSON.parse(localStorage.getItem(GROUPS_KEY) ?? 'null');
      if (Array.isArray(saved)) {
        const valid = saved.filter((k): k is Division => DIVISIONS.some((d) => d.key === k));
        if (valid.length) return valid;
      }
    } catch {
      // fall through to the default
    }
    return [...DEFAULT_DIVISIONS];
  }
  let shownGroups = $state<Division[]>(readGroups());
  function toggleGroup(key: string) {
    const k = key as Division;
    const next = shownGroups.includes(k) ? shownGroups.filter((g) => g !== k) : [...shownGroups, k];
    // Never leave the card empty -- the same rule the metric picker keeps.
    if (!next.length) return;
    shownGroups = next;
    try {
      localStorage.setItem(GROUPS_KEY, JSON.stringify(shownGroups));
    } catch {
      // Still applied for this session.
    }
  }

  const allGroups = $derived(grid.groups);
  const groups = $derived(allGroups.filter((g) => shownGroups.includes(g.key)));  const total = $derived(groups.reduce((n, g) => n + g.charts.length, 0));

  /* THE TABLE NEVER FILTERS. `includeQuiet` is a chart-grid concern — a flat
     line is a chart-sized hole — and in a table a quiet link is one short row
     that says, in the `why` column, that it is down. Hiding it there would be
     hiding the answer. */
  const allDivisions = $derived(
    buildRows(names, columns, nodeIds, active, rdma, fabric, liveFacts),
  );
  const divisions = $derived(allDivisions.filter((d) => shownGroups.includes(d.key)));

  /* Interface counts per group for the menu, from the table's grouping: it
     never filters quiet links, so it is the honest count of what exists. */
  /* The groups menu also carries what used to be two things on the card: the
     "N idle" toggle beside the view switch, and a heading for a group whose
     links are all idle. Both said "idle" on the card's face; neither belongs
     there. A group that would draw nothing in charts says "all idle" beside
     its name here, and the last row is the toggle that draws idle links. */
  const groupItems = $derived(
    DIVISIONS.map((d) => {
      const n = allDivisions.find((x) => x.key === d.key)?.rows.length ?? 0;
      const on = shownGroups.includes(d.key);
      const last = on && shownGroups.length === 1;
      const silent = n > 0 && !includeQuiet && !allGroups.some((g) => g.key === d.key);
      return {
        key: d.key,
        label: n ? `${d.label} · ${n}` : d.label,
        checked: on,
        disabled: last,
        note: last ? 'last one' : silent ? 'all idle' : undefined,
      };
    }),
  );
  const idleItem = $derived([
    {
      key: '__idle',
      label: 'Idle links',
      checked: includeQuiet,
      note: grid.quiet ? `${grid.quiet} hidden` : undefined,
    },
  ]);
  const linkCount = $derived(divisions.reduce((n, d) => n + d.rows.length, 0));
  const portCount = $derived(nodes.reduce((n, x) => n + (x.rdma?.length ?? 0), 0));

  const mode = $derived(chosenMode ?? (linkCount > TABLE_ABOVE ? 'table' : 'charts'));


  /* The ports view's columns, owned here so the card's one column menu can
     list them beside the links'. `network.rdma` is the key the RDMA Ports card
     used, so a column someone hid there stays hidden here. */
  const rdmaCols = new ColumnView('network.rdma', RDMA_COLUMNS);

  /* One menu for the card, so both divisions' tables share a column set and a
     storage key. Two menus would be two controls in two corners of one card —
     the arrangement NetworkPanel already rejected for the same reason. */
  const linkCols = new ColumnView('network-history.links', NETWORK_COLUMNS);

  /* Signal columns, forced ONCE for the shared view.
     `err` and `drop` read zero every day, which is exactly why someone switches
     them off, and their first non-zero value is the thing they needed to know.

     Computed across EVERY division rather than inside NetworkTable, because
     `linkCols` is shared by all of them: per-instance forcing had each table
     fighting the others over one piece of state and locked the page's update
     loop. See the note in NetworkTable. */
  const linkTripped = $derived.by(() => {
    const all = divisions.flatMap((d) => d.rows);
    return [
      ...(all.some((r) => r.errors > 0) ? ['err'] : []),
      ...(all.some((r) => r.drops > 0) ? ['drop'] : []),
    ];
  });
  $effect(() => linkCols.force(linkTripped));

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

  /* One page view per division grid, the way ThermalPanel keeps one per
     domain. `pageSize` is set from the effect below, never inside `pagesFor`,
     for the reason ThermalPanel spells out: this is called from the template.
     The OPEN grid in table mode is deliberately not paged -- those are the
     rows the reader clicked into, and should not vanish behind a page. */
  type Chart = (typeof groups)[number]['charts'][number];
  const pageViews = new Map<string, TableView<Chart>>();
  function pagesFor(key: string): TableView<Chart> {
    let v = pageViews.get(key);
    if (!v) {
      v = new TableView<Chart>([], Infinity);
      pageViews.set(key, v);
    }
    return v;
  }
  /* ONE ROW BUDGET FOR THE CARD, shared across its divisions in order.
     A per-grid cap does not shrink the card: Fabric has exactly two rows, so a
     cap of two cut nothing and Management's single row kept itself, and the
     card sat at three rows however far it was dragged -- measured. Divisions
     take rows from a shared budget, each reserving one row for every division
     after it so that none disappears: a division with no rows would have no
     pager, and the reader could not page to what it holds. The floor is one
     row per division. Infinity flows through as "all". */
  $effect.pre(() => {
    let remaining = plotRows;
    groups.forEach((g, i) => {
      const after = groups.length - i - 1;
      const rows = Math.min(rowsTotal(g.charts.length), Math.max(1, remaining - after));
      pagesFor(g.key).pageSize = rows * cols;
      remaining -= rows;
    });
  });
  const rowsTotal = (n: number) => Math.ceil(n / cols);

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
      /* Paired against the live snapshot for any port whose metric predates
         AC1c. Without this the `roce` column reads "—" for every fabric link
         until the node stacks are redeployed — the one column the Fabric
         division exists to explain. */
      rdma = pairPorts(ports(portRows), livePairs);
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
      <h2 class="eyebrow">Network Activity</h2>
      <!-- Stays put in every view. A port is RoCE by definition, so the
           groups do not apply there: disabled rather than hidden, so the
           legend beside it does not shift when the view changes. -->
      <PickMenu
        groups={[{ items: groupItems }, { label: 'Charts', items: idleItem }]}
        ontoggle={(key) => (key === '__idle' ? writeFlag(QUIET_KEY, (includeQuiet = !includeQuiet)) : toggleGroup(key))}
        disabled={mode === 'ports'}
        what="Interface groups"
        of="Network Activity"
        text="groups"
        count={shownGroups.length}
        countLabel={`of ${DIVISIONS.length} shown`}
        icon="list"
      />
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
      <div class="modes segmented" role="group" aria-label="View">
        {#each MODES as m (m)}
          <button
            class="range"
            class:active={mode === m}
            aria-pressed={mode === m}
            title={m === 'table'
              ? 'One row per link — the whole cluster at once'
              : m === 'ports'
                ? 'One row per RDMA port — state, negotiated rate and errors'
                : 'One chart per link, on its own axis'}
            onclick={() => setMode(m)}
          >{m}</button>
        {/each}
      </div>

      <!-- EVERY CONTROL STAYS WHERE IT IS in every view. Events and the range
           only mean something on charts and the table -- the ports view is
           live -- but a control that appears and disappears moves its
           neighbours, and a reader who has learned where "table" is finds it
           somewhere else after switching. Disabled and dimmed instead, with
           the title saying why. -->
      <button
        class="events toggle"
        class:on={showEvents && mode !== 'ports'}
        aria-pressed={showEvents}
        disabled={mode === 'ports'}
        title={mode === 'ports'
          ? 'The ports view is live; it has no window to mark events on'
          : 'Mark alerts, cold starts and agent deploys on the charts'}
        onclick={() => writeFlag(EVENTS_KEY, (showEvents = !showEvents))}
      >events{annotations.length ? ` · ${annotations.length}` : ''}</button>

      <div class="ranges segmented" role="group" aria-label="Time range">
        {#each RANGES as r (r.key)}
          <button
            class="range"
            class:active={r.key === rangeKey}
            aria-pressed={r.key === rangeKey}
            disabled={mode === 'ports'}
            title={mode === 'ports' ? 'The ports view is live; it has no time range' : undefined}
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
    {#if mode === 'ports'}
      <!-- THE FABRIC BY PORT. Live from the snapshot rather than from history
           like the other two views -- a port negotiating at the wrong rate is
           something you check, not something you watch. The groups menu does
           not apply: a port is RoCE by definition. -->
      <section class="division" data-division="ports">
        <h3 class="division-head">
          Ports
          <span class="count">{portCount}</span>
          <span class="note dim">RDMA devices and the rate each actually negotiated</span>
          <ColumnMenu of="Network Activity" groups={[{ label: 'RDMA ports', view: rdmaCols }]} />
        </h3>
        <RdmaTable {nodes} cols={rdmaCols} {maxRows} />
      </section>
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
                  height={plotHeight}
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
              <ColumnMenu of="Network Activity" groups={[{ label: 'Links', view: linkCols }]} />
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
          {@const pages = pagesFor(g.key)}
          <div
            class="charts"
            style:--cols={cols}
            style:--cols-md={colsMd}
            data-rows-total={rowsTotal(g.charts.length)}
          >
            {#each pages.slice(g.charts) as c (c.key)}
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
                  height={plotHeight}
                />
              </div>
            {/each}
          </div>
          <Pager view={pages} total={g.charts.length} label="{g.label} chart pages" />
          {/if}
        </section>
      {/each}
    </div>
    {/if}
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
    font-size: var(--text-label);
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
    font-size: var(--text-micro);
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


  /* The view switch, the range and the toggles are the shared .segmented and
     .toggle primitives in app.css; only the legend's "show all" is styled
     here. */
  .clear {
    padding: 2px 7px;
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    background: none;
    color: var(--ink-muted);
    font: inherit;
    font-size: var(--text-label);
    cursor: pointer;
  }

  .clear:hover {
    color: var(--ink-2);
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
    font-size: var(--text-label);
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
    font-size: var(--text-micro);
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
    font-size: var(--text-nano);
    letter-spacing: 0.04em;
    color: var(--ink-muted);
  }

  .empty,
  .error {
    margin: 8px 0 4px;
    font-size: var(--text-body);
  }

  .error {
    color: var(--critical);
  }
</style>
