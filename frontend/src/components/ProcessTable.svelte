<script lang="ts">
  /* GPU processes, sorted by memory — the nvitop view.
   *
   * Not every GPU consumer is an inference runtime. On GB10 image generation
   * draws on the same pool as model weights, so labelling the runtime is the
   * difference between "12GiB used, unexplained" and "12GiB used by ComfyUI".
   * Unrecognised processes still appear, just unlabelled: honest beats a
   * confident wrong guess.
   *
   * Display only. Nothing here is clickable — this dashboard never kills a
   * process, which is a deliberate non-goal, not an oversight.
   */
  import { gib, ratioPct } from '../lib/format';
  import ColumnMenu from './ColumnMenu.svelte';
  import ColumnGrip from './ColumnGrip.svelte';
  import Pager from './Pager.svelte';
  import SortButton from './SortButton.svelte';
  import { TableView, dropSortWhenHidden } from '../lib/table.svelte';
  import { ColumnView } from '../lib/columns.svelte';
  import type { ColumnDef } from '../lib/table.svelte';
  import { LLM_RUNTIMES } from '../lib/types';
  import { pageFocus } from '../lib/focus.svelte';
  import type { NodeSnapshot } from '../lib/types';

  interface Props {
    nodes: NodeSnapshot[];
    /** Rows before it pages. Infinity = uncapped. */
    maxRows?: number;
  }
  const { nodes, maxRows = 10 }: Props = $props();

  interface Row {
    key: string;
    node: string;
    pid: number;
    name: string;
    runtime: string | null;
    model: string | null;
    /** The model name was inferred from the cluster's head node rather than
     *  reported by the process itself. */
    shard: boolean;
    bytes: number;
    sharePct: number;
    smPct: number;
    encPct: number;
    decPct: number;
  }

  const rows = $derived.by<Row[]>(() => {
    const out: Row[] = [];
    for (const node of nodes) {
      if (!pageFocus.includes(node.node_id)) continue;
      const total = node.memory?.total_bytes ?? 0;
      for (const p of node.processes) {
        out.push({
          key: `${node.node_id}/${p.pid}`,
          node: node.node_id,
          pid: p.pid,
          name: p.name,
          runtime: p.runtime,
          model: p.model,
          shard: p.shard ?? false,
          bytes: p.gpu_mem_bytes,
          smPct: p.sm_pct,
          encPct: p.encoder_pct,
          decPct: p.decoder_pct,
          sharePct: ratioPct(p.gpu_mem_bytes, total),
        });
      }
    }
    return out.sort((a, b) => b.bytes - a.bytes);
  });

  /* The table's own order — biggest consumer first — remains the default, and
     cycling a header past ascending returns to it. That order answers "what is
     holding the pool", which is why anyone opens this panel.

     `runtime` and `model` are legitimately null (an unlabelled process, a
     router parent that serves every model and holds only its own overhead).
     They sort last in both directions: an unknown is not a small value, and
     letting nulls lead an ascending sort fills the top with rows that have
     nothing to say. */
  const view = new TableView<Row>([
    { key: 'name', value: (r) => r.name },
    { key: 'runtime', value: (r) => r.runtime },
    { key: 'model', value: (r) => r.model },
    { key: 'node', value: (r) => r.node },
    { key: 'pid', value: (r) => r.pid },
    { key: 'sm', value: (r) => r.smPct },
    { key: 'mem', value: (r) => r.bytes },
    /* Share, not bytes. They only rank alike when every node has the same
       pool: 8 GiB on a 128 GiB node is a smaller share than 6 GiB on a 64 GiB
       one, and this column exists to say so. */
    { key: 'share', value: (r) => r.sharePct },
  ]);

  // Before paint — see ModelsTable.
  $effect.pre(() => {
    view.pageSize = maxRows;
  });

  const shown = $derived(view.slice(rows));

  const COLUMNS: ColumnDef[] = [
    { key: 'name', label: 'process', required: true, width: 20 },
    { key: 'runtime', label: 'runtime', width: 13 },
    { key: 'model', label: 'model', width: 22 },
    { key: 'node', label: 'node', width: 13 },
    { key: 'pid', label: 'pid', right: true, width: 9 },
    { key: 'sm', label: 'sm', right: true, width: 7 },
    { key: 'mem', label: 'gpu mem', right: true, width: 11 },
    { key: 'share', label: 'share of pool', cls: 'share', width: 16 },
  ];

  const cols = new ColumnView('processes', COLUMNS);

  // Sorting by a column you just switched off would leave the rows in an order
  // nothing on screen explains.
  $effect(() => dropSortWhenHidden(view, (k) => cols.isVisible(k)));

  const totals = $derived.by(() => {
    let llm = 0;
    let other = 0;
    for (const r of rows) {
      if (r.runtime && LLM_RUNTIMES.has(r.runtime)) llm += r.bytes;
      else other += r.bytes;
    }
    return { llm, other };
  });

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
</script>

{#snippet cell(c: ColumnDef, row: Row)}
  {#if c.key === 'name'}
    <td class="name">{row.name}</td>
  {:else if c.key === 'runtime'}
    <td class="runtimecol">
      {#if row.runtime}
        <span class="runtime" data-kind={LLM_RUNTIMES.has(row.runtime) ? 'llm' : 'other'}>
          {row.runtime}
        </span>
      {:else}
        <span class="dim">unlabelled</span>
      {/if}
    </td>
  {:else if c.key === 'model'}
    <!-- A router parent legitimately has no model: it serves all of them and
         holds only its own overhead. An em dash says that plainly rather than
         implying missing data. -->
    <td
      class="modelcol"
      title={row.shard
        ? `${row.model} — shard of a model served by this node's cluster`
        : row.model || undefined}
    >
      {#if row.model}
        <span class="model">{row.model}</span>
        {#if row.shard}
          <!-- The name was filled in by the backend from the cluster's head
               node, not reported by the process. Marked so an inferred
               attribution is distinguishable from a self-reported one. -->
          <span class="dim" aria-label="cluster shard">·shard</span>
        {/if}
      {:else}
        <span class="dim">—</span>
      {/if}
    </td>
  {:else if c.key === 'node'}
    <td class="dim">{row.node}</td>
  {:else if c.key === 'pid'}
    <td class="r num dim pid">{row.pid}</td>
  {:else if c.key === 'sm'}
    <td class="r num compute">
      {#if row.smPct > 0}
        <span class="sm">{row.smPct.toFixed(0)}%</span>
      {:else}
        <!-- Absent from NVML's samples means idle, so this is a reading rather
             than missing data. Dimmed rather than blank: a resident-but-idle
             model is the interesting case, and a gap would read as
             "unknown". -->
        <span class="dim">0%</span>
      {/if}
      {#if row.encPct > 0 || row.decPct > 0}
        <!-- Encoder/decoder are separate fixed-function blocks, so this work is
             NOT competing for SM. Shown small and apart so it explains a busy
             GPU without implying contention that isn't there. -->
        <span class="codec dim" title="NVENC / NVDEC — separate from SM">
          {row.encPct > 0 ? `enc ${row.encPct.toFixed(0)}%` : ''}
          {row.decPct > 0 ? `dec ${row.decPct.toFixed(0)}%` : ''}
        </span>
      {/if}
    </td>
  {:else if c.key === 'mem'}
    <td class="r num mem">{gib(row.bytes)}</td>
  {:else if c.key === 'share'}
    <td class="share">
      <!-- An inline bar rather than a separate chart: the number and its
           magnitude belong in the same glance. -->
      <span class="bar-track">
        <span
          class="bar"
          style:width={`${Math.max(row.sharePct, 0.4)}%`}
          data-kind={row.runtime && LLM_RUNTIMES.has(row.runtime) ? 'llm' : 'other'}
        ></span>
      </span>
    </td>
  {/if}
{/snippet}

<section class="panel">
  <header>
    <h2 class="eyebrow">GPU processes</h2>
    <span class="dim count">
      {gib(totals.llm)} GiB models · {gib(totals.other)} GiB other
    </span>
    <ColumnMenu groups={[{ view: cols }]} of="GPU processes" />
  </header>

  {#if rows.length}
    <div class="scroll">
      <table>
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
          {#each cols.visible() as c (c.key)}
            <col style="width: {cols.width(c.key) !== null
              ? `${cols.width(c.key)}px`
              : `${c.width}ch`}" />
          {/each}
          <col />
        </colgroup>
        <thead>
          <tr>
            {#each cols.visible() as c (c.key)}
              <th use:register={c.key}
                scope="col"
                class:r={c.right}
                class:share={c.cls === 'share'}
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
            <!-- SLACK PARKS HERE — see the .slack rule in the style block. -->
            <th class="slack"></th>
          </tr>
        </thead>
        <tbody>
          {#each shown as row (row.key)}
            <tr>
              <!-- Cells are rendered from the SAME list as the headers above.
                   Hand-written <td>s in fixed order were the alternative, and
                   if that list and this one ever disagreed every value would
                   shift into the neighbouring column — which looks like
                   corrupted data rather than a broken table, and is therefore
                   worse than a crash. One source, no drift. -->
              {#each cols.visible() as c (c.key)}
                {@render cell(c, row)}
              {/each}
              <td class="slack"></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    <Pager {view} total={rows.length} label="GPU process pages" />
  {:else}
    <p class="empty">No processes are holding GPU memory.</p>
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

  table {
    font-size: 12px;
    min-width: 620px;
    /* Required for the colgroup widths to be honoured at all. */
    table-layout: fixed;
  }

  th {
    /* Positioning context for the resize grip, which sits on the column
       boundary rather than inside the cell's text flow. */
    position: relative;
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

  .name {
    font-weight: 500;
  }

  /* Column widths now come from the `<colgroup>` under `table-layout: fixed`
     (AA2), not from the `width: 1%` idiom this comment used to describe. The
     concern it recorded still holds and is what the declared widths are for:
     numbers spread across a wide page drift so far from the row's identity
     that tracking one across becomes unreliable. */
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

  .share {
    width: 34%;
    min-width: 120px;
  }

  .runtime[data-kind='llm'] {
    color: var(--series-1);
  }
  .runtime[data-kind='other'] {
    color: var(--series-2);
  }

  /* Monospace because it's an identifier the operator will match by eye
     against the models table and against router logs. */
  .model {
    font-family: var(--font-mono, ui-monospace, monospace);
    font-size: 11px;
  }

  /* Compute sits beside memory rather than in its own panel: the question
     "who is competing" is only meaningful next to "who is resident". */
  .compute {
    /* Raised from 96px: this column carries an OPTIONAL second line (enc/dec),
       so its content appears and disappears with the workload. Measured
       114-120px in practice, and anything under that lets the column resize. */
    min-width: 120px;
  }

  .sm {
    color: var(--series-3);
    font-weight: 500;
  }

  /* Reserved widths — see NetworkPanel for the full reasoning. A column that
     resizes as its number grows drags every other column with it under auto
     table layout. `.compute` above already reserves the SM column. */
  /* Bounded, not just reserved. These two columns are sized by the widest value
     currently on the page, and unlike a number that is a set of ROWS that comes
     and goes — a transcode starting or a model unloading changes which strings
     are present, so the column resizes and drags `share of pool` 27px with it.
     A floor stops the common case shrinking it; a ceiling with ellipsis stops
     an unusually long name expanding it. The full name is on the cell's
     title. */
  /* "107.5" — GiB to one decimal, room for a four-digit pool. */
  .codec {
    display: block;
    font-size: 10px;
    letter-spacing: 0.04em;
  }

  .bar-track {
    display: block;
    height: 6px;
    background: var(--track);
    border-radius: 2px;
    overflow: hidden;
  }

  .bar {
    display: block;
    height: 100%;
    border-radius: 2px;
    transition: width 400ms cubic-bezier(0.4, 0, 0.2, 1);
  }

  .bar[data-kind='llm'] {
    background: var(--series-1);
  }
  .bar[data-kind='other'] {
    background: var(--series-2);
  }

  .empty {
    padding: 0 16px 14px;
    font-size: 12px;
    color: var(--ink-2);
  }

  /* AA2: widths come from the `<colgroup>` above, which only `table-layout:
     fixed` actually honours — in auto layout a specified width is a suggestion
     and content can override it, so a dragged column would sometimes spring
     back and read as the drag not working.

     THE TRADE FIXED LAYOUT MAKES, and it reverses a decision AA1 took: a
     column can no longer grow to fit its content, so a long value has to be
     clipped rather than allowed to widen the table. AA1 argued against ellipsis
     on the grounds that a model whose name you cannot read is worse than a wide
     column — that was right while columns could grow. Now that they cannot, the
     alternative to ellipsis is text visibly spilling across the neighbouring
     cell, which is worse than either. The full value stays reachable: every
     truncatable cell carries it as a `title`, and the reader can widen the
     column and keep that width.

     The `.slack` column stays unsized so it still absorbs whatever
     `width: 100%` leaves over. Without it, fixed layout would spread the
     surplus across every column in proportion to the widths just set, which
     undoes the point of setting them. */

  /* THE RESERVED MIN-WIDTHS ARE GONE (AA2). They existed because these columns
     swing between an em dash and a live reading every time a model wakes or
     sleeps, and in an auto-layout table that resized the whole row on every
     transition. Under `table-layout: fixed` a column's width comes from the
     colgroup and content cannot change it, so the jitter they defended against
     is now impossible by construction rather than merely discouraged.

     Clipping and ellipsis moved to the shared `:not(.slack)` rule, which
     applies to every column rather than the few that happened to need it. */
  th:not(.slack),
  td:not(.slack) {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  th.slack,
  td.slack {
    width: auto;
  }

</style>
