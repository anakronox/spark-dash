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
  import { bitRate } from '../lib/format';
  import ColumnMenu from './ColumnMenu.svelte';
  import ColumnGrip from './ColumnGrip.svelte';
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
   *  throughput against a "200 Gb/s" link is the whole point.
   *
   * The rendering lives in lib/format so the Network Activity charts below can
   * use the same one. This panel and those charts describe the same links, and
   * a table saying "580.2 Mb/s" above a chart saying "0.58 Gb/s" would read as
   * two different measurements. */
  const bits = (bytesPerSec: number) => bitRate(bytesPerSec * 8);

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

  // Before paint — see ModelsTable.
  $effect.pre(() => {
    rdmaView.pageSize = maxRows;
  });

  const RDMA_COLUMNS: ColumnDef[] = [
    { key: 'port', label: 'rdma port', required: true, width: 18 },
    { key: 'state', label: 'state', width: 12 },
    { key: 'link', label: 'link', width: 12 },
    // "rate", not "negotiated": the longer word was setting this column's
    // width all by itself, and the value beneath it is self-evidently a rate.
    { key: 'rate', label: 'rate', width: 16 },
    { key: 'iface', label: 'interface', width: 17 },
    { key: 'node', label: 'node', width: 13 },
    /* 13ch, measured: at 11 the content box is 55px and "619 kb/s" needs 58,
       so any port above 100 kb/s clipped its own last character. Pre-existing
       and invisible until the fabric carried enough traffic to show it — found
       by comparing scrollWidth against clientWidth across every cell on the
       page, which is the check that should have caught it. A Gb/s reading is
       wider still, so the headroom is deliberate. */
    { key: 'rx', label: 'rx', right: true, width: 13 },
    { key: 'tx', label: 'tx', right: true, width: 13 },
    { key: 'err', label: 'err', right: true, signal: true, width: 8 },
  ];

  const rdmaCols = new ColumnView('network.rdma', RDMA_COLUMNS);

  /* Signal columns that currently have something to say.
   *
   * `err` and `drop` read zero every day, which is exactly why someone
   * switches them off — and their first non-zero value is the thing they needed
   * to know. Hiding a stat on a monitoring dashboard is hiding a signal, so the
   * signal wins: the column comes back on its own, and the menu says why. */
  const rdmaTripped = $derived(rdma.some((p) => p.errors > 0) ? ['err'] : []);
  $effect(() => rdmaCols.force(rdmaTripped));

  $effect(() => dropSortWhenHidden(rdmaView, (k) => rdmaCols.isVisible(k)));

  const rdmaShown = $derived(rdmaView.slice(rdma));

  /* Measured from the rendered header rather than from the stored value: a
     column still on its `ch` default has no stored pixel width, and a drag has
     to start from where the column actually is, not from a guess. */
  const headers = new Map<string, HTMLElement>();
  const gripWidth = (key: string) =>
    headers.get(key)?.getBoundingClientRect().width ?? 0;

  /** Keeps that map in step with what is actually rendered. An action rather
   *  than `bind:this` because the headers come from a loop whose membership
   *  changes as columns are hidden, and a stale node would hand the next drag
   *  a width from a column that is no longer on the page. */
  function register(node: HTMLElement, key: string) {
    headers.set(key, node);
    return {
      destroy() {
        headers.delete(key);
      },
    };
  }

  /* THE CLASS STRINGS, named here rather than inline -- see `lib/styles.md`.
     The pre-conversion audit (which is now the first step, after six silent
     regressions taught it) asks: what reaches these elements from somewhere
     other than their own class attribute? Here it was `.num` and `.dim` from
     app.css, `.count`, `.scroll`, `.empty`, `.state`, `.tag` and `.rate` from
     the style block, and -- the one that is easy to miss -- the bare `td {}`
     element selector, which styles the state cells that carry NO class at all.
     Every one of those is named below. */

  const TH_BASE =
    'relative text-left text-micro font-medium tracking-[0.1em] uppercase ' +
    'text-ink-muted px-3 pt-0 pb-[6px] border-b border-rule whitespace-nowrap';
  const TH = `${TH_BASE} overflow-hidden text-ellipsis`;
  const TH_R = `${TH} text-right`;

  const TD_BASE =
    'px-3 py-[5px] border-b [border-bottom-color:color-mix(in_srgb,var(--rule)_45%,transparent)] whitespace-nowrap';
  const TD = `${TD_BASE} overflow-hidden text-ellipsis`;

  /* Unsized so it still absorbs whatever `width: 100%` leaves over -- and
     unsized is ALL it is. Giving it only a width dropped the cell base off it
     in the other two tables, so the rules stopped short of the table's edge. */
  const SLACK_TH = `${TH_BASE} w-auto`;
  const SLACK_TD = `${TD_BASE} w-auto`;

  /* `tabular-nums` is the global `.num` helper spelled out. Throughput here is
     a live feed; proportional digits would shift the column on every poll. */
  const NUM = `${TD} text-right tabular-nums`;
  const DIM = `${TD} text-ink-muted`;
  const NAME = `${TD} font-medium`;

  /* Verbatim from the driver -- the string itself is the diagnosis when a link
     comes up at the wrong speed. */
  const RATE = `${TD} text-ink-2`;

  /* A non-zero counter is the whole reason the column is ever looked at. */
  const errCell = (bad: boolean) => (bad ? `${NUM} text-warning` : NUM);

  const STATE = 'inline-flex items-baseline gap-[6px]';

  /* THREE states, because "down" means two different things once exclusions
     exist. A watched link that is down is a fault and reads as one; before
     this it rendered in the same muted ink as an unused port, which made the
     most alarming row in the table also its quietest. A lookup rather than
     `[data-link]` rules -- greppable from the markup, at the cost of the three
     cases no longer sitting adjacent in a stylesheet. */
  const STATE_TONE = {
    up: `${STATE} text-good`,
    bad: `${STATE} text-critical`,
    quiet: `${STATE} text-ink-muted`,
  };



  const COUNT = 'text-ink-muted text-label';
  const SCROLL = 'overflow-x-auto';


  const TABLE = 'table-fixed text-body min-w-[620px]';
  const EMPTY = 'px-4 pt-0 pb-[14px] text-body text-ink-2';
</script>

{#snippet rdmaCell(c: ColumnDef, p: RdmaRow)}
  {#if c.key === 'port'}
    <td class={NAME}>{p.device}:{p.port}</td>
  {:else if c.key === 'state'}
    <td class={TD}>
      <span class={STATE_TONE[p.active ? 'up' : p.monitored ? 'bad' : 'quiet']}>
        <span aria-hidden="true">{p.active ? '●' : '○'}</span>
        {p.state || 'unknown'}
      </span>
    </td>
  {:else if c.key === 'link'}
    <!-- RoCE vs InfiniBand: same sysfs tree, different fabric. -->
    <td class={DIM}>{p.linkLayer || '—'}</td>
  {:else if c.key === 'rate'}
    <!-- Blank while down: the driver reports a placeholder rate there, and
         showing it would read as a negotiation fault. -->
    <td class={RATE} title={p.rate || undefined}>{p.rate ? shortRate(p.rate) : '—'}</td>
  {:else if c.key === 'iface'}
    <td class={DIM}>{p.iface || '—'}</td>
  {:else if c.key === 'node'}
    <td class={DIM}>{p.node}</td>
  {:else if c.key === 'rx'}
    <td class={NUM}>{bits(p.rx)}</td>
  {:else if c.key === 'tx'}
    <td class={NUM}>{bits(p.tx)}</td>
  {:else if c.key === 'err'}
    <td class={errCell(p.errors > 0)}>{p.errors}</td>
  {/if}
{/snippet}

<section class="panel">
  <header>
    <h2 class="eyebrow">RDMA ports</h2>
    <span class={COUNT}>
      {rdma.length}
      {rdma.length === 1 ? 'port' : 'ports'}
    </span>
    <ColumnMenu of="RDMA ports" groups={[{ label: 'RDMA ports', view: rdmaCols }]} />
  </header>

  {#if rdma.length}
    <div class={SCROLL}>
      <table class={TABLE}>
        <!-- WIDTHS LIVE HERE, not on the cells. Under `table-layout: fixed`
             the first row's widths decide the whole table, and a `<colgroup>`
             states them once instead of relying on whichever row happens to
             render first.

             A dragged width is pixels; the default is `ch`, which tracks the
             font — these tables set their own font-size, and a pixel default
             would be wrong the moment that changed. The `.slack` col stays
             unsized so it still absorbs whatever `width: 100%` leaves over
             (AA1); under fixed layout that surplus would otherwise be split
             across every column and undo the point of setting them. -->
        <colgroup>
          {#each rdmaCols.visible() as c (c.key)}
            <col style="width: {rdmaCols.width(c.key) !== null
              ? `${rdmaCols.width(c.key)}px`
              : `${c.width}ch`}" />
          {/each}
          <col />
        </colgroup>
        <thead>
          <tr>
            {#each rdmaCols.visible() as c (c.key)}
              <th use:register={c.key} scope="col" class={c.right ? TH_R : TH} aria-sort={rdmaView.ariaSort(c.key)}>
                <SortButton view={rdmaView} id={c.key} label={c.label} />
                <ColumnGrip
                  label={c.label}
                  width={() => gripWidth(c.key)}
                  onresize={(px) => rdmaCols.setWidth(c.key, px)}
                  onreset={() => rdmaCols.resetWidth(c.key)}
                />
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
            <th class={SLACK_TH}></th>
          </tr>
        </thead>
        <tbody>
          {#each rdmaShown as p (p.key)}
            <tr class:down={!p.active}>
              <!-- Same list as the headers — see ProcessTable. -->
              {#each rdmaCols.visible() as c (c.key)}
                {@render rdmaCell(c, p)}
              {/each}
              <td class={SLACK_TD}></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    <Pager view={rdmaView} total={rdma.length} label="RDMA port pages" />
  {:else if failures.length}
    <div class={EMPTY}>
      <p class="m-0">The network collector failed:</p>
      <ul class="mt-[6px] mb-0 pl-[18px]">
        {#each failures as f (f)}
          <li class="text-warning">{f}</li>
        {/each}
      </ul>
    </div>
  {:else if !reported}
    <p class={EMPTY}>
      This agent doesn't report network data — it predates the collector.
      Rebuild and redeploy the agent image.
    </p>
  {:else}
    <p class={EMPTY}>
      No RDMA devices found. The agent ran and saw nothing under
      <code class="text-ink">/sys/class/infiniband</code>, which is where a RoCE
      device registers even though it runs over Ethernet. Interfaces without one
      are in <strong class="text-ink-2">Network Activity</strong>.
    </p>
  {/if}
</section>

<style>
  /* THE RESIDUAL, and it is deliberate -- see `lib/styles.md`. Everything that
     converted cleanly is a named constant in the script above, with its
     reasoning attached. What is left is what a selector says better than a
     utility does: all four are descendant or structural selectors that no
     per-element utility can express. */

  /* The last row drops its rule so each table ends on data rather than a line.
     Both tables, one rule -- as a variant it is a `:last-child` selector spelled
     out on every row of both. */
  tbody tr:last-child td {
    border-bottom: none;
  }

  /* Row hover, on the ROW rather than the cell. With identity columns on the
     left and numbers on the right, the eye needs something to hold the line
     across the gap. */
  tbody tr:hover {
    background: var(--panel-raised);
  }

  /* A down row recedes, whichever table it is in. The state is on the ROW and
     the colour belongs to its cells; the alternative threads a flag into all
     sixteen cell branches. */
  tr.down td {
    color: var(--ink-muted);
  }

  /* Section chrome stays until phase 4 converts App.svelte, which owns the
     layout these belong to. */
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
</style>
