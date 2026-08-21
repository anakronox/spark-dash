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
    { key: 'name', label: 'process', required: true },
    { key: 'runtime', label: 'runtime' },
    { key: 'model', label: 'model' },
    { key: 'node', label: 'node' },
    { key: 'pid', label: 'pid', right: true },
    { key: 'sm', label: 'sm', right: true },
    { key: 'mem', label: 'gpu mem', right: true },
    { key: 'share', label: 'share of pool', cls: 'share' },
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
        <thead>
          <tr>
            {#each cols.visible() as c (c.key)}
              <th
                scope="col"
                class:r={c.right}
                class:share={c.cls === 'share'}
                aria-sort={view.ariaSort(c.key)}
              >
                <SortButton {view} id={c.key} label={c.label} />
              </th>
            {/each}
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

  th.r,
  td.r {
    width: 1%;
    white-space: nowrap;
  }

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
  .pid {
    min-width: calc(8ch + 24px);
  }

  /* Bounded, not just reserved. These two columns are sized by the widest value
     currently on the page, and unlike a number that is a set of ROWS that comes
     and goes — a transcode starting or a model unloading changes which strings
     are present, so the column resizes and drags `share of pool` 27px with it.
     A floor stops the common case shrinking it; a ceiling with ellipsis stops
     an unusually long name expanding it. The full name is on the cell's
     title. */
  .runtimecol {
    min-width: calc(10ch + 24px);
  }

  .modelcol {
    min-width: calc(12ch + 24px);
    max-width: calc(18ch + 24px);
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* "107.5" — GiB to one decimal, room for a four-digit pool. */
  .mem {
    min-width: calc(6ch + 24px);
  }

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
</style>
