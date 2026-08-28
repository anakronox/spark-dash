<script module lang="ts">
  import type { ColumnDef } from '../lib/table.svelte';

  /* THE COLUMN DEFS LIVE HERE, where tests/test_table_columns.py reads them.
     That guard checks every def has a cell renderer and a sort value, and it
     exists because a renderer with no def shipped twice and displayed nothing
     for two days. Moving these to a .ts module makes the check silently stop
     covering this table. */
  export const THERMAL_COLUMNS: ColumnDef[] = [
    { key: 'sensor', label: 'sensor', required: true, width: 26 },
    { key: 'node', label: 'node', width: 13 },
    { key: 'now', label: 'now', right: true, width: 10 },
    { key: 'limit', label: 'limit', right: true, width: 10 },
    // Sorts ASCENDING by default is not something a ColumnDef can say, so the
    // table's resting order handles it — see `ranked` below.
    { key: 'headroom', label: 'headroom', right: true, width: 12 },
    { key: 'bar', label: 'to limit', width: 16 },
  ];
</script>

<script lang="ts">
  /* Every temperature sensor on the cluster, ranked by what is closest to its
   * own limit.
   *
   * WHY THIS EXISTS. The dashboard reported two temperatures — the GPU, and one
   * CPU reading psutil happened to pick first — while a GB10 exposes 18 to 23.
   * Measured over 24h here, `acpitz` zone0 peaked at 95.4 °C while the GPU read
   * 72.0 °C at the same instant. There was a sensor 23 degrees hotter than the
   * one the dashboard led with, and nothing looking at it.
   *
   * RANKED BY HEADROOM, NOT BY TEMPERATURE. The limits differ by twenty degrees
   * across one box — 104.8 for a package zone, 84.85 for the nvme, 105 for a
   * NIC asic, 90 for the GPU — so temperature alone is not a ranking. Sorted by
   * degrees, an 85 °C GPU heads the table while a 52 °C NIC sits at the bottom;
   * by headroom the GPU has 5 degrees left and the NIC has 53, which is the
   * order that answers "what is closest to trouble".
   *
   * Live, from the agent's own snapshot, because a temperature you are looking
   * at should be the current one. History is the chip row on the History card.
   */
  import ColumnGrip from './ColumnGrip.svelte';
  import ColumnMenu from './ColumnMenu.svelte';
  import Pager from './Pager.svelte';
  import SortButton from './SortButton.svelte';
  import { ColumnView } from '../lib/columns.svelte';
  import { TableView, dropSortWhenHidden } from '../lib/table.svelte';
  import { byHeadroom, groupRows, hottest, tempRows, tightest } from '../lib/thermal';
  import type { TempRow } from '../lib/thermal';
  import type { NodeSnapshot } from '../lib/types';
  import { nodeColor } from '../lib/theme';

  interface Props {
    nodes: NodeSnapshot[];
    /** Rows before each domain's table pages. Infinity = uncapped. */
    maxRows?: number;
  }
  const { nodes, maxRows = 8 }: Props = $props();

  const rows = $derived(tempRows(nodes));
  const groups = $derived(groupRows(rows));
  const slots = $derived(new Map(nodes.map((n, i) => [n.node_id, i])));

  /* TWO HEADLINES, because they answer different questions and are usually
     different sensors. The hottest thing in the box is not the thing closest to
     failing: a 52 °C NIC rated to 105 is cooler and safer than an 85 °C GPU
     rated to 90. Showing one and calling it "system temperature" is what the
     single CPU number used to do. */
  const top = $derived(hottest(rows));
  const tight = $derived(tightest(rows));

  /* ONE VIEW PER DOMAIN, not one for the card. Each domain draws its own
     table, and a shared view would give them a shared page index and a shared
     sort — so paging Package to row 9 would silently page Storage past the end
     of its three rows, and clicking a header in one would reorder all of them.
     Created on demand and kept, so the sort survives a re-render. */
  const views = new Map<string, TableView<TempRow>>();
  function viewFor(domain: string): TableView<TempRow> {
    let v = views.get(domain);
    if (!v) {
      v = new TableView<TempRow>([
        { key: 'sensor', value: (r) => r.sensor },
        { key: 'node', value: (r) => r.node },
        { key: 'now', value: (r) => r.celsius },
        { key: 'limit', value: (r) => r.limitC },
        { key: 'headroom', value: (r) => r.headroomC },
        // The bar draws headroom, so it sorts by headroom. A bar that sorted
        // by anything other than its own length would be a control that lies.
        { key: 'bar', value: (r) => r.headroomC },
      ]);
      views.set(domain, v);
    }
    v.pageSize = maxRows;
    return v;
  }

  /* ONE column view for the card, though — the columns are identical in every
     domain, and a reader who hides `limit` in Package wants it hidden in
     Storage too. Two menus would be two controls in two corners of one card,
     which is the arrangement NetworkPanel already rejected. */
  const cols = new ColumnView('thermal.sensors', THERMAL_COLUMNS);
  $effect(() => {
    for (const v of views.values()) {
      dropSortWhenHidden(v, (k) => cols.visible().some((c) => c.key === k));
    }
  });

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

  const fmt = (c: number | null) => (c == null ? '—' : `${c.toFixed(1)}°`);

  /** How close a sensor is to its own limit, 0-1, for the bar.
   *
   * ANCHORED AT 40 °C, not at zero. Every sensor in a running machine sits
   * between 40 and 105, so a bar measured from absolute zero would show every
   * row at roughly half full and never move. 40 is below the coldest reading
   * observed here (41.8 °C) so nothing clips at the bottom. */
  const FLOOR_C = 40;
  function fraction(r: TempRow): number {
    if (r.limitC == null || r.limitC <= FLOOR_C) return 0;
    return Math.max(0, Math.min(1, (r.celsius - FLOOR_C) / (r.limitC - FLOOR_C)));
  }

  /* STATUS COLOURS, and this is what they are reserved for. The node palette
     was deliberately moved out of these hues so that green/amber/red on this
     dashboard always means a state — which is exactly what a sensor's distance
     from its own thermal limit is. */
  function tone(r: TempRow): string {
    if (r.headroomC == null) return 'text-ink-muted';
    if (r.headroomC <= 5) return 'text-critical';
    if (r.headroomC <= 15) return 'text-warning';
    return 'text-good';
  }

  /* Resting order is headroom, ascending — least first. `TableView.sorted`
     leaves rows untouched until a header is clicked, and they arrive already
     ranked, so clicking a header sorts by it and clicking off returns here. */
  const ordered = (v: TableView<TempRow>, rs: TempRow[]) =>
    v.sortKey ? rs : [...rs].sort(byHeadroom);

  /* THE CLASS STRINGS, named here rather than inline — see lib/styles.md. What
     reaches these elements from elsewhere: `.num` and `.dim` from app.css, the
     bare `table`/`th`/`td` element rules, and `.panel`. */
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

  /* `table-fixed`, or the declared widths are advisory — under auto layout the
     browser sizes from content and the ColumnDef numbers do nothing. */
  const TABLE = 'table-fixed text-body min-w-[620px]';
</script>

{#snippet cell(c: ColumnDef, r: TempRow)}
  {#if c.key === 'sensor'}
    <td class={TD}>
      <!-- Verbatim. `zone0` and `mlx5 0000:01:00.0 asic` are what the kernel
           calls these, and a prettified name is one nobody can grep for. -->
      <span class="[font-variant-numeric:tabular-nums]">{r.sensor}</span>
    </td>
  {:else if c.key === 'node'}
    <td class={DIM}>
      <span class="inline-block w-[7px] h-[7px] rounded-[2px] mr-[6px] align-[0px]"
            style:background={nodeColor(slots.get(r.node))}></span>{r.node}
    </td>
  {:else if c.key === 'now'}
    <td class={NUM}>{fmt(r.celsius)}</td>
  {:else if c.key === 'limit'}
    <!-- Blank, never zero, where the hardware states no limit. A wifi phy with
         a limit of 0 would report the worst headroom on the card. -->
    <td class={NUM_DIM}>{fmt(r.limitC)}</td>
  {:else if c.key === 'headroom'}
    <td class={`${NUM} ${tone(r)}`}>{r.headroomC == null ? '—' : `${r.headroomC.toFixed(1)}°`}</td>
  {:else if c.key === 'bar'}
    <td class={TD}>
      <span class="block h-[6px] bg-track rounded-[2px] overflow-hidden">
        <span
          class="block h-full rounded-[2px] transition-[width] duration-[400ms]"
          style:width="{(fraction(r) * 100).toFixed(1)}%"
          style:background={r.headroomC == null
            ? 'var(--ink-muted)'
            : r.headroomC <= 5
              ? 'var(--critical)'
              : r.headroomC <= 15
                ? 'var(--warning)'
                : 'var(--good)'}
        ></span>
      </span>
    </td>
  {/if}
{/snippet}

<section class="panel">
  <header>
    <h2 class="eyebrow">Temperatures</h2>
    {#if top}
      <!-- BOTH headlines, side by side. "Hottest" and "closest to its limit"
           are usually different sensors, and a card that showed only the first
           would repeat the mistake the single CPU number made. -->
      <span class="head">
        <span class="stat">
          <span class="k">hottest</span>
          <span class="v num">{fmt(top.celsius)}</span>
          <span class="who">{top.sensor} · {top.node}</span>
        </span>
        {#if tight}
          <span class="stat">
            <span class="k">closest to limit</span>
            <span class="v num {tone(tight)}">{tight.headroomC?.toFixed(1)}° left</span>
            <span class="who">{tight.sensor} · {tight.node}</span>
          </span>
        {/if}
      </span>
    {/if}
    <ColumnMenu of="Temperatures" groups={[{ label: 'Sensors', view: cols }]} />
  </header>

  {#if !rows.length}
    <p class="empty dim">
      No sensors reported. The agent reads <code>/sys/class/thermal</code> and
      <code>/sys/class/hwmon</code> — this needs an agent from 2026-08-25 or later,
      with the host's <code>/sys</code> mounted.
    </p>
  {:else}
    {#each groups as g (g.key)}
      {@const view = viewFor(g.key)}
      {@const shown = view.slice(ordered(view, g.rows))}
      <section class="domain">
        <h3 class="head-row">
          {g.label}
          <span class="count">{g.rows.length}</span>
          <span class="note dim">{g.note}</span>
        </h3>
        <div class="scroll">
          <table class={TABLE}>
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
                <th class={SLACK_TH}></th>
              </tr>
            </thead>
            <tbody>
              {#each shown as r (r.key)}
                <tr>
                  {#each cols.visible() as c (c.key)}
                    {@render cell(c, r)}
                  {/each}
                  <td class={SLACK_TD}></td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
        <Pager {view} total={g.rows.length} label="{g.label} pages" />
      </section>
    {/each}
  {/if}
</section>

<style>
  section.panel {
    padding: 12px 16px 12px;
  }

  header {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 6px 14px;
    margin-bottom: 8px;
  }

  h2 {
    margin: 0;
  }

  .head {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 18px;
    margin-right: auto;
  }

  .stat {
    display: inline-flex;
    align-items: baseline;
    gap: 6px;
  }

  .stat .k {
    font-size: var(--text-micro);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-muted);
  }

  .stat .v {
    font-size: var(--text-body);
    font-weight: 600;
    color: var(--ink);
  }

  .stat .who {
    font-size: var(--text-micro);
    color: var(--ink-muted);
  }

  .domain + .domain {
    margin-top: 12px;
  }

  .head-row {
    display: flex;
    align-items: baseline;
    gap: 8px;
    margin: 0 0 6px;
    padding-bottom: 4px;
    /* A rule rather than a heavier weight — the card already has a title and
       two headline stats above this. */
    border-bottom: 1px solid var(--rule);
    font-size: var(--text-label);
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-2);
  }

  .head-row .count {
    font-weight: 400;
    color: var(--ink-muted);
    font-variant-numeric: tabular-nums;
  }

  .head-row .note {
    margin-left: auto;
    font-weight: 400;
    font-size: var(--text-micro);
    letter-spacing: 0.02em;
    text-transform: none;
  }

  @media (max-width: 700px) {
    .head-row .note {
      display: none;
    }
  }

  .scroll {
    overflow-x: auto;
  }

  .empty {
    margin: 8px 0 4px;
    font-size: var(--text-body);
  }
</style>
