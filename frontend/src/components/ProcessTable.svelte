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

  /* THE CLASS STRINGS, named and reasoned about here rather than inline --
     see `lib/styles.md`. The audit that has to happen before any component is
     converted (four regressions taught this) is: which declarations reach
     these elements from somewhere OTHER than their own class attribute?
     For this file they were `.num` and `.dim` from app.css, and `.count`,
     `.empty` and `.scroll` from the style block below. All five are named
     below; none is left to a class the markup carries and no rule defines. */

  const TH =
    /* `pt-0` is not decoration: `padding: 0 12px 6px` says "nothing above" out
       loud, and with no preflight the UA's own `th { padding: 1px }` fills any
       silence. The truncation pair came from `th:not(.slack), td:not(.slack)`
       -- ONE rule covering both cell types, which is exactly how the th half
       went missing in ModelsTable. */
    'relative text-left text-micro font-medium tracking-[0.1em] uppercase ' +
    'text-ink-muted px-3 pt-0 pb-[6px] border-b border-rule ' +
    'whitespace-nowrap overflow-hidden text-ellipsis';

  /* Body cells carry the rule at 45% so the grid recedes behind the data. */
  const TD =
    'px-3 py-[5px] border-b [border-bottom-color:color-mix(in_srgb,var(--rule)_45%,transparent)] ' +
    'whitespace-nowrap overflow-hidden text-ellipsis';

  /* `tabular-nums` is the global `.num` helper, spelled out. Dropping it is
     invisible until the numbers change and the column starts shifting. */
  const NUM = `${TD} text-right tabular-nums`;
  const DIM = `${TD} text-ink-muted`;
  const NAME = `${TD} font-medium`;
  const NUM_DIM = `${NUM} text-ink-muted`;

  /* Reserved because this column carries an OPTIONAL second line (enc/dec)
     that appears and disappears with the workload. Measured 114-120px. */
  const COMPUTE = `${NUM} min-w-[120px]`;

  /* Share is 34% by design -- the bar needs room to be readable as a
     magnitude, which is the only reason this column exists. */
  const SHARE = `${TD} w-[34%] min-w-[120px]`;
  const SHARE_TH = `${TH} w-[34%] min-w-[120px]`;

  /* The trailing column that absorbs whatever `width: 100%` leaves over.
     Unsized on purpose, and deliberately NOT built on TD: it is excluded from
     the truncation rule because there is nothing in it to truncate. */
  const SLACK = 'w-auto';

  const MUTED = 'text-ink-muted';

  /* Monospace stated explicitly even though the page is already monospace:
     this is an identifier the operator matches by eye against the models
     table and against router logs, and it should survive a change to the
     body font. */
  const MODEL_NAME = 'font-mono text-label';
  const SM = 'text-series-3 font-medium';
  const CODEC = 'block text-micro tracking-[0.04em] text-ink-muted';

  /* An LLM runtime and everything else get the SAME two colours here as on
     the history chart, so a spike and the process that drew it match without
     a legend. A lookup rather than `[data-kind]` rules, for the reason
     StatusPill records: the mapping becomes greppable from the markup and
     stops being exhaustive-by-adjacency. */
  const RUNTIME_TONE = { llm: 'text-series-1', other: 'text-series-2' };
  const BAR_TONE = { llm: 'bg-series-1', other: 'bg-series-2' };
  const kind = (runtime: string | null): 'llm' | 'other' =>
    runtime && LLM_RUNTIMES.has(runtime) ? 'llm' : 'other';
</script>

{#snippet cell(c: ColumnDef, row: Row)}
  {#if c.key === 'name'}
    <td class={NAME}>{row.name}</td>
  {:else if c.key === 'runtime'}
    <td class={TD}>
      {#if row.runtime}
        <span class={RUNTIME_TONE[kind(row.runtime)]}>
          {row.runtime}
        </span>
      {:else}
        <span class={MUTED}>unlabelled</span>
      {/if}
    </td>
  {:else if c.key === 'model'}
    <!-- A router parent legitimately has no model: it serves all of them and
         holds only its own overhead. An em dash says that plainly rather than
         implying missing data. -->
    <td
      class={TD}
      title={row.shard
        ? `${row.model} — shard of a model served by this node's cluster`
        : row.model || undefined}
    >
      {#if row.model}
        <span class={MODEL_NAME}>{row.model}</span>
        {#if row.shard}
          <!-- The name was filled in by the backend from the cluster's head
               node, not reported by the process. Marked so an inferred
               attribution is distinguishable from a self-reported one. -->
          <span class={MUTED} aria-label="cluster shard">·shard</span>
        {/if}
      {:else}
        <span class={MUTED}>—</span>
      {/if}
    </td>
  {:else if c.key === 'node'}
    <td class={DIM}>{row.node}</td>
  {:else if c.key === 'pid'}
    <td class={NUM_DIM}>{row.pid}</td>
  {:else if c.key === 'sm'}
    <td class={COMPUTE}>
      {#if row.smPct > 0}
        <span class={SM}>{row.smPct.toFixed(0)}%</span>
      {:else}
        <!-- Absent from NVML's samples means idle, so this is a reading rather
             than missing data. Dimmed rather than blank: a resident-but-idle
             model is the interesting case, and a gap would read as
             "unknown". -->
        <span class={MUTED}>0%</span>
      {/if}
      {#if row.encPct > 0 || row.decPct > 0}
        <!-- Encoder/decoder are separate fixed-function blocks, so this work is
             NOT competing for SM. Shown small and apart so it explains a busy
             GPU without implying contention that isn't there. -->
        <span class={CODEC} title="NVENC / NVDEC — separate from SM">
          {row.encPct > 0 ? `enc ${row.encPct.toFixed(0)}%` : ''}
          {row.decPct > 0 ? `dec ${row.decPct.toFixed(0)}%` : ''}
        </span>
      {/if}
    </td>
  {:else if c.key === 'mem'}
    <td class={NUM}>{gib(row.bytes)}</td>
  {:else if c.key === 'share'}
    <td class={SHARE}>
      <!-- An inline bar rather than a separate chart: the number and its
           magnitude belong in the same glance. -->
      <span class="block h-[6px] bg-track rounded-[2px] overflow-hidden">
        <span
          class="block h-full rounded-[2px] transition-[width] duration-[400ms] ease-[cubic-bezier(0.4,0,0.2,1)] {BAR_TONE[kind(row.runtime)]}"
          style:width={`${Math.max(row.sharePct, 0.4)}%`}
        ></span>
      </span>
    </td>
  {/if}
{/snippet}

<section class="panel">
  <header>
    <h2 class="eyebrow">GPU processes</h2>
    <span class="text-ink-muted text-label">
      {gib(totals.llm)} GiB models · {gib(totals.other)} GiB other
    </span>
    <ColumnMenu groups={[{ view: cols }]} of="GPU processes" />
  </header>

  {#if rows.length}
    <div class="overflow-x-auto">
      <table class="table-fixed text-body min-w-[620px]">
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
                class={c.cls === 'share' ? SHARE_TH : c.right ? `${TH} text-right` : TH}
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
            <th class={SLACK}></th>
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
              <td class={SLACK}></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    <Pager {view} total={rows.length} label="GPU process pages" />
  {:else}
    <p class="px-4 pt-0 pb-[14px] text-body text-ink-2">No processes are holding GPU memory.</p>
  {/if}
</section>

<style>
  /* THE RESIDUAL, and it is deliberate -- see `lib/styles.md`. Everything that
     converted cleanly is a named constant in the script above, with its
     reasoning attached. What is left is what a selector says better than a
     utility does. */

  /* Structural: the last row drops its rule so the table ends on data rather
     than on a line. As a variant this is `[&:last-child>td]:border-b-0` on
     every row, which is longer and says less. */
  tbody tr:last-child td {
    border-bottom: none;
  }

  /* Row hover, and it earns its place: with the identity columns on the left
     and the numbers on the right, the eye needs something to hold the line
     across the gap between them. On the ROW rather than the cell, which no
     per-cell utility can do. */
  tbody tr:hover {
    background: var(--panel-raised);
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
