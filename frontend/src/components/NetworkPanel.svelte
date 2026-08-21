<script lang="ts">
  /* Network interfaces and RDMA ports.
   *
   * RDMA leads when present, because on a clustered pair it's the interconnect
   * the distributed inference actually rides — a degraded RoCE link is the
   * difference between a model spanning two nodes usefully and not.
   *
   * The negotiated rate is shown verbatim rather than parsed into a number.
   * A ConnectX-7 that comes up at 10 Gb/sec instead of 200 is a known and
   * otherwise invisible failure, and the driver's own string is the clearest
   * statement of what actually happened.
   */
  import { num } from '../lib/format';
  import ColumnMenu from './ColumnMenu.svelte';
  import Pager from './Pager.svelte';
  import SortButton from './SortButton.svelte';
  import { TableView, dropSortWhenHidden } from '../lib/table.svelte';
  import { ColumnView } from '../lib/columns.svelte';
  import type { ColumnDef } from '../lib/table.svelte';
  import type { NodeSnapshot } from '../lib/types';
  import { pageFocus } from '../lib/focus.svelte';

  interface Props {
    nodes: NodeSnapshot[];
    /** Rows before each of the two tables pages. Infinity = uncapped.
     *  Applied per table, not shared: they answer different questions and an
     *  RDMA port list is not competing with an interface list for the cap. */
    maxRows?: number;
  }
  const { nodes, maxRows = 8 }: Props = $props();

  interface IfaceRow {
    key: string;
    node: string;
    name: string;
    up: boolean;
    monitored: boolean;
    speedMbps: number | null;
    rx: number;
    tx: number;
    errors: number;
    dropped: number;
  }

  interface RdmaRow {
    key: string;
    node: string;
    device: string;
    port: number;
    monitored: boolean;
    state: string;
    linkLayer: string;
    rate: string;
    iface: string;
    rx: number;
    tx: number;
    errors: number;
    active: boolean;
  }

  const interfaces = $derived.by<IfaceRow[]>(() =>
    nodes.filter((n) => pageFocus.includes(n.node_id)).flatMap((n) =>
      (n.network ?? []).map((i) => ({
        key: `${n.node_id}/${i.name}`,
        node: n.node_id,
        name: i.name,
        up: i.up,
        /* Defaulted true for a snapshot from an agent that predates the flag:
           an older agent watches everything, which is what the field means. */
        monitored: i.monitored ?? true,
        speedMbps: i.speed_mbps,
        rx: i.rx_bytes_per_sec,
        tx: i.tx_bytes_per_sec,
        errors: i.rx_errors + i.tx_errors,
        dropped: i.rx_dropped + i.tx_dropped,
      })),
    ),
  );

  /* Collector failures, surfaced verbatim.
   *
   * The previous empty state asserted a cause — "mount /proc and /sys" — that
   * it had no way of knowing. When the mounts were already right that sent the
   * reader after the wrong thing entirely. The agent records why each
   * collector failed; showing that is strictly better than a guess. */
  const failures = $derived.by<string[]>(() => {
    const out: string[] = [];
    for (const n of nodes) {
      for (const key of ['network', 'rdma']) {
        const message = n.errors?.[key];
        if (message) out.push(`${n.node_id}: ${key} — ${message}`);
      }
    }
    return out;
  });

  /** True when at least one node reported the field at all. An older agent
   *  omits it entirely, which is a different problem from finding nothing. */
  const reported = $derived(nodes.some((n) => Array.isArray(n.network)));

  const rdma = $derived.by<RdmaRow[]>(() =>
    nodes.filter((n) => pageFocus.includes(n.node_id)).flatMap((n) =>
      (n.rdma ?? []).map((p) => ({
        key: `${n.node_id}/${p.device}/${p.port}`,
        node: n.node_id,
        device: p.device,
        port: p.port,
        monitored: p.monitored ?? true,
        state: p.state,
        linkLayer: p.link_layer,
        rate: p.rate,
        iface: p.interface,
        rx: p.rx_bytes_per_sec,
        tx: p.tx_bytes_per_sec,
        errors: p.errors,
        active: p.state.toUpperCase().endsWith('ACTIVE'),
      })),
    ),
  );

  /** Bytes/sec as bits/sec — network gear is rated in bits, and comparing
   *  throughput against a "200 Gb/s" link is the whole point. */
  function bits(bytesPerSec: number): string {
    const b = bytesPerSec * 8;
    if (b >= 1e9) return `${num(b / 1e9, 2)} Gb/s`;
    if (b >= 1e6) return `${num(b / 1e6, 1)} Mb/s`;
    if (b >= 1e3) return `${num(b / 1e3, 0)} kb/s`;
    return `${num(b, 0)} b/s`;
  }

  /** The driver's rate, abbreviated for the cell. Full string on the title.
   *
   * Both rules are anchored so they can only touch what they name — the unit,
   * and a parenthetical at the very end. A rate phrased in some form this does
   * not recognise passes through untouched rather than mangled, which is the
   * property that matters for a string whose whole job is to report what
   * actually happened.
   */
  function shortRate(rate: string): string {
    return rate.replace(/\/sec\b/gi, '/s').replace(/\s*\([^)]*\)\s*$/, '');
  }

  function speed(mbps: number | null): string {
    if (mbps === null) return '—';
    return mbps >= 1000 ? `${num(mbps / 1000, 0)}G` : `${num(mbps, 0)}M`;
  }

  /** The negotiated rate as a number, for ordering only — the cell still shows
   *  the driver's own string.
   *
   * Sorting the string itself is actively wrong: "100 Gb/sec" sorts BEFORE
   * "40 Gb/sec" lexically, so the slowest link would head a descending sort.
   * That is the exact failure this column exists to catch — a ConnectX-7 that
   * came up at 10 Gb/sec instead of 200 — so a sort that buries it is worse
   * than no sort at all.
   *
   * Unparseable rates sort last rather than as zero: a rate the driver phrased
   * in a form not seen here is unknown, not slow.
   */
  function rateGbps(rate: string): number | null {
    const m = /([\d.]+)\s*(T|G|M)b/i.exec(rate);
    if (!m) return null;
    const value = Number(m[1]);
    if (!Number.isFinite(value)) return null;
    const unit = m[2].toUpperCase();
    return unit === 'T' ? value * 1000 : unit === 'M' ? value / 1000 : value;
  }

  /* One view per table. Both keep the agent's own order as the default — which
     is per node, then as the kernel enumerates them — and cycling a header
     past ascending returns to it. */
  const rdmaView = new TableView<RdmaRow>([
    { key: 'port', value: (r) => `${r.device}:${r.port}` },
    { key: 'state', value: (r) => r.state },
    { key: 'link', value: (r) => r.linkLayer },
    { key: 'rate', value: (r) => rateGbps(r.rate) },
    { key: 'iface', value: (r) => r.iface },
    { key: 'node', value: (r) => r.node },
    { key: 'rx', value: (r) => r.rx },
    { key: 'tx', value: (r) => r.tx },
    { key: 'err', value: (r) => r.errors },
  ]);

  const ifaceView = new TableView<IfaceRow>([
    { key: 'name', value: (r) => r.name },
    { key: 'node', value: (r) => r.node },
    { key: 'link', value: (r) => r.speedMbps },
    { key: 'rx', value: (r) => r.rx },
    { key: 'tx', value: (r) => r.tx },
    { key: 'err', value: (r) => r.errors },
    { key: 'drop', value: (r) => r.dropped },
  ]);

  // Before paint — see ModelsTable.
  $effect.pre(() => {
    rdmaView.pageSize = maxRows;
    ifaceView.pageSize = maxRows;
  });

  const RDMA_COLUMNS: ColumnDef[] = [
    { key: 'port', label: 'rdma port', required: true },
    { key: 'state', label: 'state' },
    { key: 'link', label: 'link' },
    // "rate", not "negotiated": the longer word was setting this column's
    // width all by itself, and the value beneath it is self-evidently a rate.
    { key: 'rate', label: 'rate' },
    { key: 'iface', label: 'interface' },
    { key: 'node', label: 'node' },
    { key: 'rx', label: 'rx', right: true },
    { key: 'tx', label: 'tx', right: true },
    { key: 'err', label: 'err', right: true, signal: true },
  ];

  const IFACE_COLUMNS: ColumnDef[] = [
    { key: 'name', label: 'interface', required: true },
    { key: 'node', label: 'node' },
    { key: 'link', label: 'link', right: true },
    { key: 'rx', label: 'rx', right: true },
    { key: 'tx', label: 'tx', right: true },
    { key: 'err', label: 'err', right: true, signal: true },
    { key: 'drop', label: 'drop', right: true, signal: true },
  ];

  const rdmaCols = new ColumnView('network.rdma', RDMA_COLUMNS);
  const ifaceCols = new ColumnView('network.ifaces', IFACE_COLUMNS);

  /* Signal columns that currently have something to say.
   *
   * `err` and `drop` read zero every day, which is exactly why someone
   * switches them off — and their first non-zero value is the thing they needed
   * to know. Hiding a stat on a monitoring dashboard is hiding a signal, so the
   * signal wins: the column comes back on its own, and the menu says why. */
  const rdmaTripped = $derived(rdma.some((p) => p.errors > 0) ? ['err'] : []);
  const ifaceTripped = $derived([
    ...(interfaces.some((i) => i.errors > 0) ? ['err'] : []),
    ...(interfaces.some((i) => i.dropped > 0) ? ['drop'] : []),
  ]);

  $effect(() => rdmaCols.force(rdmaTripped));
  $effect(() => ifaceCols.force(ifaceTripped));

  $effect(() => dropSortWhenHidden(rdmaView, (k) => rdmaCols.isVisible(k)));
  $effect(() => dropSortWhenHidden(ifaceView, (k) => ifaceCols.isVisible(k)));

  /** green when up, red when a WATCHED link is down, muted when a link nobody
   *  watches is down. Three states because "down" means two different things
   *  once exclusions exist, and rendering them alike is what made a real
   *  failure the quietest row in the table. */
  function linkTone(i: { up: boolean; monitored: boolean }): 'up' | 'bad' | 'quiet' {
    if (i.up) return 'up';
    return i.monitored ? 'bad' : 'quiet';
  }

  const rdmaShown = $derived(rdmaView.slice(rdma));
  const ifacesShown = $derived(ifaceView.slice(interfaces));
</script>

{#snippet rdmaCell(c: ColumnDef, p: RdmaRow)}
  {#if c.key === 'port'}
    <td class="name">{p.device}:{p.port}</td>
  {:else if c.key === 'state'}
    <td>
      <span class="state" data-link={p.active ? 'up' : p.monitored ? 'bad' : 'quiet'}>
        <span aria-hidden="true">{p.active ? '●' : '○'}</span>
        {p.state || 'unknown'}
      </span>
    </td>
  {:else if c.key === 'link'}
    <!-- RoCE vs InfiniBand: same sysfs tree, different fabric. -->
    <td class="dim">{p.linkLayer || '—'}</td>
  {:else if c.key === 'rate'}
    <!-- Blank while down: the driver reports a placeholder rate there, and
         showing it would read as a negotiation fault. -->
    <td class="rate" title={p.rate || undefined}>{p.rate ? shortRate(p.rate) : '—'}</td>
  {:else if c.key === 'iface'}
    <td class="dim">{p.iface || '—'}</td>
  {:else if c.key === 'node'}
    <td class="dim">{p.node}</td>
  {:else if c.key === 'rx'}
    <td class="r num rate-col">{bits(p.rx)}</td>
  {:else if c.key === 'tx'}
    <td class="r num rate-col">{bits(p.tx)}</td>
  {:else if c.key === 'err'}
    <td class="r num errs" class:bad={p.errors > 0}>{p.errors}</td>
  {/if}
{/snippet}

{#snippet ifaceCell(c: ColumnDef, i: IfaceRow)}
  {#if c.key === 'name'}
    <td class="name">
      <!-- COLOUR IS THE LINK, the tag is whether anyone is watching.
           Down-and-watched is `bad`, not muted: before this, a failed link
           rendered in the same grey as an unused port, so the most alarming
           row in the table was also its quietest. Down-and-excluded stays
           muted, because that one really is unremarkable. -->
      <span class="state" data-link={linkTone(i)}>
        <span aria-hidden="true">{i.up ? '●' : '○'}</span>
        {i.name}
      </span>
      {#if !i.monitored}
        <span class="tag" title="Excluded from alerting in this node's config">
          not monitored
        </span>
      {/if}
    </td>
  {:else if c.key === 'node'}
    <td class="dim">{i.node}</td>
  {:else if c.key === 'link'}
    <td class="r num dim linkspeed">{speed(i.speedMbps)}</td>
  {:else if c.key === 'rx'}
    <td class="r num rate-col">{bits(i.rx)}</td>
  {:else if c.key === 'tx'}
    <td class="r num rate-col">{bits(i.tx)}</td>
  {:else if c.key === 'err'}
    <td class="r num errs" class:bad={i.errors > 0}>{i.errors}</td>
  {:else if c.key === 'drop'}
    <td class="r num errs" class:bad={i.dropped > 0}>{i.dropped}</td>
  {/if}
{/snippet}

<section class="panel">
  <header>
    <h2 class="eyebrow">Network</h2>
    <span class="dim count">
      {interfaces.length}
      {interfaces.length === 1 ? 'interface' : 'interfaces'}
      {#if rdma.length}
        · {rdma.length} RDMA
      {/if}
    </span>
    <!-- ONE control for a card that draws two tables, with the groups named.
         Two buttons would mean two corners on one card. -->
    <ColumnMenu
      of="Network"
      groups={[
        { label: 'RDMA ports', view: rdmaCols },
        { label: 'Interfaces', view: ifaceCols },
      ]}
    />
  </header>

  {#if rdma.length}
    <div class="scroll">
      <table>
        <thead>
          <tr>
            {#each rdmaCols.visible() as c (c.key)}
              <th scope="col" class:r={c.right} aria-sort={rdmaView.ariaSort(c.key)}>
                <SortButton view={rdmaView} id={c.key} label={c.label} />
              </th>
            {/each}
            <!-- SLACK PARKS HERE. `table { width: 100% }` in app.css forces the
                 table to fill its container, and in an auto-layout table that
                 surplus is handed to whichever columns can grow — in
                 proportion to their content, so the column with the longest
                 strings takes most of it. That is what opened a large gap
                 beside `model` once M3 added three more content-sized numeric
                 columns and left even more slack to redistribute.

                 An empty final column with no width constraint absorbs it
                 instead, so every real column sizes to its own content. Not
                 `aria-hidden`: an empty `th`/`td` pair is already announced as
                 an empty cell, and hiding it would leave the row's cell count
                 disagreeing with the header's. -->
            <th class="slack"></th>
          </tr>
        </thead>
        <tbody>
          {#each rdmaShown as p (p.key)}
            <tr class:down={!p.active}>
              <!-- Same list as the headers — see ProcessTable. -->
              {#each rdmaCols.visible() as c (c.key)}
                {@render rdmaCell(c, p)}
              {/each}
              <td class="slack"></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    <Pager view={rdmaView} total={rdma.length} label="RDMA port pages" />
  {/if}

  {#if interfaces.length}
    <div class="scroll" class:spaced={rdma.length > 0}>
      <table>
        <thead>
          <tr>
            {#each ifaceCols.visible() as c (c.key)}
              <th scope="col" class:r={c.right} aria-sort={ifaceView.ariaSort(c.key)}>
                <SortButton view={ifaceView} id={c.key} label={c.label} />
              </th>
            {/each}
            <!-- SLACK PARKS HERE. `table { width: 100% }` in app.css forces the
                 table to fill its container, and in an auto-layout table that
                 surplus is handed to whichever columns can grow — in
                 proportion to their content, so the column with the longest
                 strings takes most of it. That is what opened a large gap
                 beside `model` once M3 added three more content-sized numeric
                 columns and left even more slack to redistribute.

                 An empty final column with no width constraint absorbs it
                 instead, so every real column sizes to its own content. Not
                 `aria-hidden`: an empty `th`/`td` pair is already announced as
                 an empty cell, and hiding it would leave the row's cell count
                 disagreeing with the header's. -->
            <th class="slack"></th>
          </tr>
        </thead>
        <tbody>
          {#each ifacesShown as i (i.key)}
            <tr class:down={!i.up}>
              {#each ifaceCols.visible() as c (c.key)}
                {@render ifaceCell(c, i)}
              {/each}
              <td class="slack"></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    <Pager view={ifaceView} total={interfaces.length} label="Interface pages" />
  {:else if failures.length}
    <div class="empty">
      <p>The network collector failed:</p>
      <ul>
        {#each failures as f (f)}
          <li>{f}</li>
        {/each}
      </ul>
    </div>
  {:else if !reported}
    <p class="empty">
      This agent doesn't report network data — it predates the collector.
      Rebuild and redeploy the agent image.
    </p>
  {:else}
    <p class="empty">
      No physical interfaces found. The agent ran but saw nothing with a
      <code>device</code> entry under <code>/sys/class/net</code>, which is how
      a real NIC is told from a virtual one.
    </p>
  {/if}
</section>

<style>
  section {
    padding: 14px 0 4px;
  }

  header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    padding: 0 16px 10px;
  }

  .count {
    font-size: 11px;
  }

  .scroll {
    overflow-x: auto;
  }

  .scroll.spaced {
    margin-top: 14px;
    border-top: 1px solid var(--rule);
    padding-top: 10px;
  }

  table {
    font-size: 12px;
    min-width: 620px;
  }

  th {
    text-align: left;
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-muted);
    padding: 0 12px 6px;
    border-bottom: 1px solid var(--rule);
    white-space: nowrap;
  }

  td {
    padding: 5px 12px;
    border-bottom: 1px solid color-mix(in srgb, var(--rule) 45%, transparent);
    white-space: nowrap;
  }

  tbody tr:last-child td {
    border-bottom: none;
  }

  tr.down td {
    color: var(--ink-muted);
  }

  .name {
    font-weight: 500;
  }

  /* Numeric columns shrink to their contents instead of sharing the slack.
     `width: 1%` with nowrap is the standard way to say "as narrow as the text
     allows" in an auto-layout table.

     Without it a wider page spreads every column equally, which pushed the
     numbers so far from the row's identity that tracking one across became
     unreliable — the exact failure the old 1180px cap was hiding. The slack
     now lands in the text columns, where longer names and addresses can use
     it, and the numbers stay in one readable block. */
  /* Row hover. Cheap, and it is what makes a wide table navigable: with the
     identity columns on the left and the numbers on the right, the eye needs
     something to hold the line across the gap between them. */
  tbody tr:hover {
    background: var(--panel-raised);
  }

  /* Width and nowrap now come from the `:not(.slack)` rule below, which
     covers every column rather than only the numeric ones. This keeps the
     alignment, which is all it was ever uniquely doing. */
  .r {
    text-align: right;
  }

  .state {
    display: inline-flex;
    align-items: baseline;
    gap: 6px;
  }

  .state[data-link='up'] {
    color: var(--good);
  }

  /* A watched link that is down is a fault, and reads as one. Before this it
     rendered in the same muted ink as an unused port. */
  .state[data-link='bad'] {
    color: var(--critical);
  }

  /* A link nobody watches, down, is exactly as unremarkable as it looks. */
  .state[data-link='quiet'] {
    color: var(--ink-muted);
  }

  /* Sits beside the name rather than taking a column of its own: it is true of
     a minority of rows, and a column would spend width on emptiness. */
  .tag {
    margin-left: 6px;
    font-size: 9px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 1px 5px;
    border-radius: var(--radius);
    border: 1px solid var(--rule);
    color: var(--ink-muted);
    white-space: nowrap;
  }

  /* RESERVED WIDTHS, so a rising number cannot resize its own column.
     Throughput is the whole problem: `bits()` returns anything from "0 b/s" to
     "200.00 Gb/s", and measured live the rx column swung 66px to 81px as
     traffic moved. That 14px pushed the table from 813px to 825px inside an
     813px box, so the horizontal scrollbar appeared and disappeared — and auto
     table layout redistributed the difference, wobbling every OTHER column by
     2-4px at the same time. One volatile column shifts the whole row.

     `ch` is exact here because these cells are monospace with tabular figures,
     so one `ch` is one digit at any zoom or font stack.

     THE `+ 24px` IS NOT PADDING FOR ERROR — it is the cell's own padding.
     These cells are `border-box`, so a bare `min-width: 11ch` reserves 11
     characters INCLUDING the 5px/12px padding, leaving about 7.7ch for the
     number. The first attempt at this fix did exactly that and the columns
     went on resizing, because the reservation was narrower than the content it
     was meant to cover. The padding has to be added back explicitly.

     11ch covers "200.00 Gb/s" — the widest reading a 200Gb link can produce,
     and the widest `bits()` emits below terabit. Sized to the HARDWARE rather
     than to the formatter's theoretical maximum, because every character
     reserved here is a character taken from the interface name beside it. */
  .rate-col {
    min-width: calc(11ch + 24px);
  }

  /* Counters. Five digits, which is the last 7px the RDMA table needed to stop
     overflowing its column at half width — and a cheap 7px, because this is a
     number read as "is it zero", not one read digit by digit. Past 99,999 the
     column grows once and stays grown. That is not the failure this reservation
     guards against: the point is that it cannot OSCILLATE between two widths on
     a live feed, and an error count crossing 100k is a one-way trip. */
  .errs {
    min-width: calc(5ch + 24px);
  }

  /* "100G", "10G" or an em dash. */
  .linkspeed {
    min-width: calc(5ch + 24px);
  }

  /* Verbatim from the driver — the string itself is the diagnosis when a link
     comes up at the wrong speed. Truncated for WIDTH only, never for content:
     the full value is on the cell's title, and the rate leads the string so
     what gets clipped is the parenthetical, not the number. */
  /* No max-width or ellipsis any more: `shortRate` bounds the string itself, so
     the column is sized by content that is already short rather than by a cap
     that clips a longer one. "200 Gb/s" is the widest it can be. */
  .rate {
    color: var(--ink-2);
  }

  .bad {
    color: var(--warning);
  }

  .empty {
    padding: 0 16px 14px;
    font-size: 12px;
    color: var(--ink-2);
  }

  .empty p {
    margin: 0;
  }

  .empty ul {
    margin: 6px 0 0;
    padding-left: 18px;
  }

  .empty li {
    color: var(--warning);
  }

  code {
    color: var(--ink);
  }

  /* Real columns size to their content; the trailing `.slack` column takes
     whatever `width: 100%` leaves over. `width: 1%` with nowrap is the
     auto-layout idiom for "as narrow as your content allows" — the numeric
     columns already used it, and applying it to the text columns too is what
     stops one of them absorbing the entire surplus.

     A pathological name still gets its full width rather than being truncated,
     which is why there is no ellipsis here: this is a monitoring table, and a
     model you cannot read the name of is not better than a wide column. If a
     name is long enough to force horizontal scroll, the `.scroll` wrapper
     handles it.

     AA2 replaces all of this with `table-layout: fixed` and explicit
     per-column widths; until then this is the smallest change that makes the
     default readable. */
  th:not(.slack),
  td:not(.slack) {
    width: 1%;
    white-space: nowrap;
  }

  th.slack,
  td.slack {
    width: auto;
  }

</style>
