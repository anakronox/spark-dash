<script module lang="ts">
  import type { ColumnDef } from '../lib/table.svelte';

  /* THE COLUMN DEFS LIVE HERE, exported, and that is deliberate. They are read
     out of this file by tests/test_table_columns.py, which checks that every
     def has a cell renderer and a sort value — the guard that exists because a
     renderer with no def shipped twice and displayed nothing for two days. Move
     them to a .ts module and that check silently stops covering this table. */
  export const NETWORK_COLUMNS: ColumnDef[] = [
    /* 32ch, and MEASURED rather than guessed twice.
       18 truncated the `not monitored` tag beside a 13-character device name to
       "not m…". 26 was the second guess and still clipped the longest pair —
       and the check that passed it was reading `textContent`, which a CSS
       `text-overflow` ellipsis never appears in. Comparing `scrollWidth`
       against `clientWidth` on the rendered cells put the requirement at 222px
       against 188px available, which is where 32 comes from. */
    { key: 'iface', label: 'interface', required: true, width: 32 },
    { key: 'node', label: 'node', width: 13 },
    // Negotiated speed. Sorts numerically, so a link that came up at a tenth
    // of its rating heads a descending sort instead of being buried by a
    // lexical one — the same reasoning the RDMA table's `rate` column uses.
    { key: 'link', label: 'link', right: true, width: 10 },
    // The sparkline sorts by BURSTINESS, which is what its shape shows. Sorting
    // it by peak would put a flat line at the top of a column whose whole job
    // is to show movement.
    /* 16ch, measured the same way as `iface`. At 14 the cell's content box was
       77px against an 80px sparkline, so the last 3px sat under the cell's
       overflow-hidden — and the right-hand end of a sparkline is the most
       recent samples, which is the part worth seeing. Widening rather than
       shrinking SPARK_W: the sparkline is the column. */
    { key: 'trend', label: 'trend', width: 16 },
    { key: 'peak', label: 'peak', right: true, width: 14 },
    { key: 'now', label: 'now', right: true, width: 14 },
    { key: 'err', label: 'err', right: true, signal: true, width: 8 },
    { key: 'drop', label: 'drop', right: true, signal: true, width: 9 },
    { key: 'port', label: 'roce', width: 10 },
    { key: 'why', label: 'why', width: 14 },
  ];
</script>

<script lang="ts">
  /* Every link on one screen, whatever the cluster size.
   *
   * WHY THIS EXISTS. The chart grid draws one small multiple per interface,
   * which is the right way to read a fabric whose links span six orders of
   * magnitude — and its cost grows with the cluster. A fully-populated GB10 has
   * 6 interfaces and 4 RDMA ports, so 32 nodes is ~190 links and, at 7d with
   * faults and port states, ~500 charts. The wall is not the data (five range
   * queries serve the card however many links it draws) but uPlot INSTANCES:
   * every chart is a canvas plus a ResizeObserver.
   *
   * A table costs the same at 3 nodes and at 32. Nothing is hidden — every link
   * is a row — and the default sort is what makes it an at-a-glance view rather
   * than a list: the rows that need attention are at the top, and the `why`
   * column says which rule put them there.
   *
   * The sparklines are SVG paths, never uPlot. A canvas per row would reproduce
   * exactly the cost this exists to escape.
   */
  import ColumnGrip from './ColumnGrip.svelte';
  import Pager from './Pager.svelte';
  import SortButton from './SortButton.svelte';
  import type { ColumnView } from '../lib/columns.svelte';
  import { TableView, dropSortWhenHidden } from '../lib/table.svelte';
  import { bitRate } from '../lib/format';
  import { byImportance, sparkPath } from '../lib/network-history';
  import type { LinkRow } from '../lib/network-history';
  import { nodeColor } from '../lib/theme';

  interface Props {
    label: string;
    rows: LinkRow[];
    /** ONE view for the whole card, not one per division. The columns are
     *  identical, and a reader who hides `now` in Fabric wants it hidden in
     *  Management too — two menus would also mean two controls in two corners
     *  of one card, which is the arrangement NetworkPanel already rejected. */
    cols: ColumnView;
    /** Identity slot per node id, so a row's sparkline is the colour its charts
     *  and its card already use. */
    slots: Map<string, number>;
    /** Rows before the table pages. Infinity = uncapped. */
    maxRows?: number;
    /** Links whose full chart is open above this table. */
    open: ReadonlySet<string>;
    ontoggle: (key: string) => void;
  }
  const { label, rows, cols, slots, maxRows = 8, open, ontoggle }: Props = $props();

  /* The sort keys, one per column key — see tests/test_table_columns.py, which
     exists because a renderer without a matching ColumnDef shipped twice and
     rendered nothing at all for two days. */
  const view = new TableView<LinkRow>([
    { key: 'iface', value: (r) => r.iface },
    { key: 'node', value: (r) => r.node },
    { key: 'link', value: (r) => r.speedMbps },
    { key: 'trend', value: (r) => r.burst },
    { key: 'peak', value: (r) => r.peak },
    { key: 'now', value: (r) => r.now },
    { key: 'err', value: (r) => r.errors },
    { key: 'drop', value: (r) => r.drops },
    { key: 'port', value: (r) => r.port ?? '' },
    { key: 'why', value: (r) => r.tier },
  ]);

  $effect.pre(() => {
    view.pageSize = maxRows;
  });


  /* Signal columns with something to say — the rule the Network table already
     follows. `err` and `drop` read zero every day, which is exactly why someone
     switches them off, and their first non-zero value is the thing they needed
     to know. */
  const tripped = $derived([
    ...(rows.some((r) => r.errors > 0) ? ['err'] : []),
    ...(rows.some((r) => r.drops > 0) ? ['drop'] : []),
  ]);
  $effect(() => cols.force(tripped));
  $effect(() => dropSortWhenHidden(view, (k) => cols.visible().some((c) => c.key === k)));

  /* Importance is the DEFAULT, not a fixed order: `TableView.sorted` returns
     the rows untouched until a header is clicked, and the rows arrive already
     ranked. Clicking a header then sorts by that column, and clicking it off
     returns to importance — which is the behaviour every other table here has,
     with a more useful resting state. */
  const ranked = $derived(view.sortKey ? rows : [...rows].sort(byImportance));
  const shown = $derived(view.slice(ranked));

  const SPARK_W = 80;
  const SPARK_H = 14;

  /** Tone for the roce cell. Status colours, deliberately: this IS a status,
   *  and it is the one column on the row that carries one. */
  const PORT_TONE: Record<string, string> = {
    up: 'text-good',
    flapped: 'text-warning',
    down: 'text-critical',
  };

  const headers = new Map<string, HTMLElement>();
  const gripWidth = (key: string) => headers.get(key)?.getBoundingClientRect().width ?? 0;
  function register(node: HTMLElement, key: string) {
    headers.set(key, node);
    return {
      destroy() {
        headers.delete(key);
      },
    };
  }

  /* THE CLASS STRINGS, named here rather than inline — see lib/styles.md. The
     pre-conversion audit asks what reaches these elements from anywhere other
     than their own class attribute. Here it is `.num` and `.dim` from app.css,
     the bare `table`/`th`/`td` element rules, and `.panel`. */
  const TH_BASE =
    'relative text-left text-micro font-medium tracking-[0.1em] uppercase ' +
    'text-ink-muted px-3 pt-0 pb-[6px] border-b border-rule whitespace-nowrap';
  const TH = `${TH_BASE} overflow-hidden text-ellipsis`;
  const TH_R = `${TH} text-right`;
  const SLACK_TH = `${TH_BASE} w-auto`;

  const TD_BASE =
    'px-3 py-[5px] border-b ' +
    '[border-bottom-color:color-mix(in_srgb,var(--rule)_45%,transparent)] whitespace-nowrap';
  const TD = `${TD_BASE} overflow-hidden text-ellipsis`;
  const SLACK_TD = `${TD_BASE} w-auto`;
  const NUM = `${TD} text-right [font-variant-numeric:tabular-nums]`;
  const DIM = `${TD} text-ink-muted`;
  const NUM_DIM = `${NUM} text-ink-muted`;

  const errCell = (hot: boolean) => `${NUM} ${hot ? 'text-warning' : 'text-ink-muted'}`;
  const TAG =
    'ml-[6px] px-[5px] py-[1px] rounded-[3px] text-micro tracking-[0.04em] ' +
    'bg-[color-mix(in_srgb,var(--ink)_8%,transparent)] text-ink-muted align-[1px]';

  /** Negotiated speed, abbreviated. `—` when the driver reports none, which is
   *  every wifi port here — absent is not zero. */
  function speed(mbps: number | null): string {
    if (mbps === null) return '—';
    return mbps >= 1000 ? `${Math.round(mbps / 1000)}G` : `${Math.round(mbps)}M`;
  }

  /* `table-fixed`, or the declared widths are advisory: under auto layout the
     browser sizes columns from content and the ColumnDef numbers do nothing.
     `min-w` so the columns keep their proportions on a narrow card and the
     `.scroll` wrapper takes over instead of everything crushing. */
  const TABLE = 'table-fixed text-body min-w-[800px]';
</script>

{#snippet cell(c: ColumnDef, r: LinkRow)}
  {#if c.key === 'iface'}
    <td class={TD}>
      <!-- The interface name keeps its case: `enP7s7` is what `ip link` says. -->
      <span class="[font-variant-numeric:tabular-nums]">{r.iface}</span>
      <!-- A TAG, not a column. It applies to a minority of links and says
           something about CONFIG rather than about traffic, so a column of
           mostly-blank cells would cost a column's width to say nothing most of
           the time. It is here because a reader looking at a bad link needs to
           know it is not paging anyone. -->
      {#if !r.monitored}
        <span class={TAG} title="Excluded from alerting in this node's config">
          not monitored
        </span>
      {/if}
    </td>
  {:else if c.key === 'node'}
    <td class={DIM}>{r.node}</td>
  {:else if c.key === 'link'}
    <td class={NUM_DIM}>{speed(r.speedMbps)}</td>
  {:else if c.key === 'trend'}
    <td class={TD}>
      <!-- Scaled to the row's own maximum, like the small multiples: a shared
           scale would flatten every fabric link against the management port,
           which is the problem this card already solved once. -->
      <svg
        class="block"
        width={SPARK_W}
        height={SPARK_H}
        viewBox="0 0 {SPARK_W} {SPARK_H}"
        aria-hidden="true"
      >
        <path
          d={sparkPath(r.series, SPARK_W, SPARK_H)}
          fill="none"
          stroke={nodeColor(slots.get(r.node))}
          stroke-width="1.25"
          stroke-linejoin="round"
        />
      </svg>
    </td>
  {:else if c.key === 'peak'}
    <td class={NUM}>{bitRate(r.peak)}</td>
  {:else if c.key === 'now'}
    <td class={NUM_DIM}>{bitRate(r.now)}</td>
  {:else if c.key === 'err'}
    <td class={errCell(r.errors > 0)}>{Math.round(r.errors)}</td>
  {:else if c.key === 'drop'}
    <td class={errCell(r.drops > 0)}>{Math.round(r.drops)}</td>
  {:else if c.key === 'port'}
    <!-- Blank, not "up", when there is no RoCE device here. A column that
         reported "up" for an interface with no RDMA port would invent one. -->
    <td class={r.port ? `${TD} ${PORT_TONE[r.port]}` : DIM}>{r.port ?? '—'}</td>
  {:else if c.key === 'why'}
    <!-- THE SORT, EXPLAINED. A ranking nobody can account for reads as the data
         being wrong — the same argument `dropSortWhenHidden` makes about an
         invisible sort. This names the rule that placed the row. -->
    <td class={r.why ? `${TD} text-ink-2` : DIM}>{r.why || '—'}</td>
  {/if}
{/snippet}

<!-- No wrapper element. The card already renders a `section.division` around
     this with the heading in it, and nesting a second one under the same name
     gave `.division` two meanings and made every query on the page return each
     division twice. -->
<div class="scroll">
  <table class={TABLE}>
    <!-- WIDTHS LIVE HERE, not on the cells. Under `table-layout: fixed` the
         first row's widths decide the whole table, and a `<colgroup>` states
         them once instead of depending on whichever row renders first.

         A dragged width is PIXELS and the default is `ch`: this table sets
         its own font-size, so a pixel default would be wrong the moment that
         changed — but a drag was a measurement at a specific size on a
         specific screen, and re-expressing it in `ch` would be inventing
         precision. The slack col stays unsized so it still absorbs whatever
         `width: 100%` leaves over. -->
    <colgroup>
      {#each cols.visible() as c (c.key)}
        <col
          style="width: {cols.width(c.key) !== null
            ? `${cols.width(c.key)}px`
            : `${c.width}ch`}"
        />
      {/each}
      <col />
    </colgroup>
    <thead>
      <tr>
        {#each cols.visible() as c (c.key)}
          <th
            use:register={c.key}
            scope="col"
            class={c.right ? TH_R : TH}
            aria-sort={view.ariaSort(c.key)}
          >
            <SortButton {view} id={c.key} label={c.label} />
            <ColumnGrip
              label={c.label}
              width={() => gripWidth(c.key)}
              onresize={(px) => cols.setWidth(c.key, px)}
              onreset={() => cols.resetWidth(c.key)}
            />
          </th>
        {/each}
        <!-- Slack parks here, as in the other tables: `table { width: 100% }`
             hands surplus to whichever columns can grow, which is what opens
             a gap beside the widest text column. -->
        <th class={SLACK_TH}></th>
      </tr>
    </thead>
    <tbody>
      {#each shown as r (r.key)}
        <!-- The ROW is the control. A separate button column would be a
             second thing to aim at for an action that belongs to the whole
             row, and every row already has to be hoverable to be readable. -->
        <tr
          class="row"
          class:open={open.has(r.key)}
          class:down={!r.up}
          aria-expanded={open.has(r.key)}
          tabindex="0"
          role="button"
          title={open.has(r.key) ? 'Close this chart' : 'Open this link’s chart'}
          onclick={() => ontoggle(r.key)}
          onkeydown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              ontoggle(r.key);
            }
          }}
        >
          {#each cols.visible() as c (c.key)}
            {@render cell(c, r)}
          {/each}
          <td class={SLACK_TD}></td>
        </tr>
      {/each}
    </tbody>
  </table>
</div>

<Pager {view} total={rows.length} label="{label} pages" />

<style>
  .scroll {
    /* Wide content scrolls inside its own container so the page body never
       scrolls sideways. */
    overflow-x: auto;
  }

  .row {
    cursor: pointer;
  }

  .row:hover,
  .row:focus-visible {
    background: color-mix(in srgb, var(--ink) 5%, transparent);
  }

  /* An open row is marked on the row itself, not only by the chart appearing
     somewhere above it — with a long table the chart can be off screen, and
     clicking a row then looks like it did nothing. */
  .row.open {
    background: color-mix(in srgb, var(--series-1) 12%, transparent);
    box-shadow: inset 2px 0 0 var(--series-1);
  }

  /* A link that was down at some point in the window. Recessive rather than
     alarming: the `why` column already says so in words, and this is the
     at-a-glance echo of it. */
  .row.down :global(td) {
    color: var(--ink-muted);
  }
</style>
