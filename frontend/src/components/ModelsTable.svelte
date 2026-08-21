<script lang="ts">
  /* What's running where.
   *
   * A table, not a chart: model names are identity, and there are more of them
   * than any colour scheme can carry. Assigning hues would blow past the
   * 8-slot ceiling and repaint on every router eviction.
   *
   * Every registered model appears, not just resident ones. `sleeping` is the
   * state a loaded/not-loaded boolean would hide, and it's the one worth
   * knowing: a warm process with weights released answers faster than a cold
   * start but holds almost no memory.
   */
  import { onMount } from 'svelte';
  import { fetchWithTimeout } from '../lib/request';
  import { MODEL_GLYPH, gib, num } from '../lib/format';
  import Pager from './Pager.svelte';
  import ColumnMenu from './ColumnMenu.svelte';
  import ColumnGrip from './ColumnGrip.svelte';
  import SortButton from './SortButton.svelte';
  import { TableView, dropSortWhenHidden } from '../lib/table.svelte';
  import { ColumnView } from '../lib/columns.svelte';
  import type { ColumnDef } from '../lib/table.svelte';
  import type { NodeSnapshot, ModelState } from '../lib/types';
  import { engines } from '../lib/types';
  import { pageFocus } from '../lib/focus.svelte';

  interface Props {
    nodes: NodeSnapshot[];
    /** Rows before it pages. Infinity = uncapped. */
    maxRows?: number;
  }
  const { nodes, maxRows = 10 }: Props = $props();

  interface Row {
    key: string;
    /** The node, or the CLUSTER when a model spans one. A distributed model is
     *  served by the cluster, so naming a single node would pick one shard's
     *  host and call it the answer. */
    node: string;
    /** Nodes this model's weights actually sit on, when there is more than
     *  one. Empty for the ordinary single-node case. */
    shardNodes: string[];
    runtime: string;
    server: string;
    model: string;
    state: ModelState;
    rawStatus: string;
    tokensPerSec: number | null;
    promptTokensPerSec: number | null;
    kvCachePct: number | null;
    running: number;
    waiting: number;
    sizeBytes: number | null;
    nParams: number | null;
    quantization: string | null;
    contextLength: number | null;
  }

  /* How long each model last took to come up, keyed by model name.
     Reconstructed server-side from the one-hot state series; see
     timeline.summarise_loads for why the answer is a multiple of the step. */
  let loadTimes = $state<Record<string, { seconds: number; uncertainty_s: number }>>({});

  async function fetchLoadTimes() {
    try {
      /* 15s, matching the scrape interval, because the duration IS the sample
         count — the estimator stays honest at any step but a coarse one widens
         the error bar past usefulness. Measured on this cluster, real loads
         occupy one or two 15s samples, so SwapTimeline's 60-600s steps cannot
         resolve them at all.

         24h because the question is "how long does this take", and a model
         that last loaded this morning still answers it. */
      const resp = await fetchWithTimeout('/api/models/timeline?minutes=1440&step=15s');
      if (!resp.ok) throw new Error(String(resp.status));
      loadTimes = (await resp.json()).loads ?? {};
    } catch {
      // Leave the previous answer up. A load time going briefly stale is
      // harmless; blanking the column on one failed poll is just flicker.
    }
  }

  onMount(() => {
    fetchLoadTimes();
    // Loads are rare — a dozen a day on this cluster. Polling faster would ask
    // Prometheus for 24h of 15s samples to learn nothing.
    const timer = setInterval(fetchLoadTimes, 300_000);
    return () => clearInterval(timer);
  });

  /* Quantisation, parameters and context window are real but secondary, and
     M3's position is that columns are chosen rather than accumulated. They ride
     in the size cell's tooltip instead of widening a table whose column widths
     were hard-won. */
  function loadDetail(row: Row): string {
    const l = loadTimes[row.model];
    if (!l) return 'no load observed in the last 24h';
    return `last load ${Math.round(l.seconds)}s ±${Math.round(l.uncertainty_s)}s (scrape resolution)`;
  }

  function sizeDetail(row: Row): string {
    const parts: string[] = [];
    if (row.nParams) parts.push(`${(row.nParams / 1e9).toFixed(1)}B params`);
    if (row.quantization) parts.push(row.quantization);
    if (row.contextLength) parts.push(`${(row.contextLength / 1024).toFixed(0)}K ctx`);
    return parts.join(' · ');
  }

  const STATE_ORDER: Record<ModelState, number> = {
    active: 0,
    loading: 1,
    sleeping: 2,
    unknown: 3,
    unloaded: 4,
  };

  const rows = $derived.by<Row[]>(() => {
    /* WHERE A MODEL'S WEIGHTS ACTUALLY SIT, keyed by the thing that serves it.
     *
     * A tensor-parallel model is split across a cluster: on `danflashes`,
     * `deepseek-v4-flash-0731` is 96.8 GiB on each of two nodes -- 193.6 GiB of
     * weights. Only the head node has an endpoint, so only IT produces a row,
     * and that row previously showed no size at all: the engines expose no
     * equivalent of llama.cpp's `meta`, so a vLLM row's footprint has to come
     * from the GPU process table.
     *
     * Summed over the CLUSTER, because the shards are parts of one model
     * rather than copies of it. Keyed by cluster (falling back to the node) so
     * a standalone node is unaffected.
     *
     * The worker's shard is in here at all only because the backend names it
     * from the head node -- an agent on a worker has no endpoint to learn the
     * model from. See `attribute_cluster_shards`. */
    const footprint = new Map<string, { bytes: number; nodes: Set<string> }>();
    for (const n of nodes) {
      for (const proc of n.processes ?? []) {
        if (!proc.model || !proc.runtime) continue;
        const k = `${n.cluster ?? n.node_id}/${proc.runtime}/${proc.model}`;
        const e = footprint.get(k) ?? { bytes: 0, nodes: new Set<string>() };
        e.bytes += proc.gpu_mem_bytes;
        e.nodes.add(n.node_id);
        footprint.set(k, e);
      }
    }

    /* Turns a footprint entry into the row fields describing WHERE the model
       lives. One node reports that node and no shard list; a model spanning a
       cluster reports the cluster and names its shards. */
    const spread = (e: { bytes: number; nodes: Set<string> } | undefined) => {
      if (!e) return { sizeBytes: null as number | null, shardNodes: [] as string[] };
      const hosts = [...e.nodes].sort();
      return { sizeBytes: e.bytes, shardNodes: hosts.length > 1 ? hosts : [] };
    };

    let out: Row[] = [];
    for (const node of nodes) {
      /* Page-level scope, applied at the source rather than after the rows are
         built: a row that is filtered out should not reach sorting, paging or
         the "N models" count, or the header would disagree with the table. */
      if (!pageFocus.includes(node.node_id)) continue;
      for (const router of node.runtimes.llama_cpp) {
        for (const m of router.models) {
          out.push({
            key: `${node.node_id}/${router.endpoint}/${m.name}`,
            node: node.node_id,
            runtime: 'llama.cpp',
            shardNodes: [],
            server: router.name || router.endpoint,
            model: m.name,
            state: m.state,
            rawStatus: m.raw_status,
            /* DECODE. `tokens_per_sec` is prefill+decode and reads three
               orders of magnitude high while a prompt is being ingested. */
            tokensPerSec: m.generation_tokens_per_sec,
            promptTokensPerSec: m.prompt_tokens_per_sec,
            kvCachePct: m.kv_cache_pct,
            running: m.requests_running,
            waiting: m.requests_waiting,
            sizeBytes: m.size_bytes,
            nParams: m.n_params,
            quantization: m.quantization,
            contextLength: m.context_length,
          });
        }
      }
      for (const [runtime, instances] of engines(node.runtimes)) {
        for (const v of instances) {
          out.push({
            key: `${node.cluster ?? node.node_id}/${runtime}/${v.model}`,
            /* The CLUSTER when the model spans one: naming a single node would
               pick one shard's host and present it as the answer. */
            node:
              (footprint.get(`${node.cluster ?? node.node_id}/${runtime}/${v.model}`)?.nodes
                .size ?? 1) > 1
                ? (node.cluster ?? node.node_id)
                : node.node_id,
            runtime,
            // Nothing fronts these engines, so an instance's own endpoint is
            // where the model is served from. Showing a dash here made it look
            // like missing data rather than a different topology.
            server: v.server || '—',
            model: v.model,
            state: 'active',
            rawStatus: '',
            tokensPerSec: v.generation_tokens_per_sec,
            promptTokensPerSec: v.prompt_tokens_per_sec,
            // Null on an SGLang row, deliberately — see EngineMetrics.
            kvCachePct: v.kv_cache_pct,
            running: v.requests_running,
            waiting: v.requests_waiting,
            ...spread(footprint.get(`${node.cluster ?? node.node_id}/${runtime}/${v.model}`)),
            nParams: null,
            quantization: null,
            contextLength: null,
          });
        }
      }
    }
    /* IDLE MODELS, hidden on request. Measured at four nodes: 32 of 36 rows
       were `unloaded` — registered with a router and holding nothing. They are
       worth being able to see (that is how you know a model exists at all) and
       worth being able to hide, which is why this is a toggle and not a
       permanent filter. `sleeping` survives it: a slept model holds a process
       and comes back fast, which is operationally different from cold. */
    if (pageFocus.hideIdleModels) {
      out = out.filter((r) => r.state !== 'unloaded');
    }

    // Active first: what's serving right now is what you came to see.
    return out.sort(
      (a, b) => STATE_ORDER[a.state] - STATE_ORDER[b.state] || a.model.localeCompare(b.model),
    );
  });

  /* Sorting and pagination. The table keeps its own order as the default —
     state first, so what is serving leads — and sorting is an override you can
     cycle back out of. Ten rows a page keeps the section a fixed height
     whether the cluster is one node or thirty-two. */
  const view = new TableView<Row>([
    { key: 'model', value: (r) => r.model },
    { key: 'state', value: (r) => STATE_ORDER[r.state] },
    { key: 'node', value: (r) => r.node },
    { key: 'server', value: (r) => r.server },
    { key: 'tok', value: (r) => r.tokensPerSec },
    { key: 'pre', value: (r) => r.promptTokensPerSec },
    { key: 'kv', value: (r) => r.kvCachePct },
    { key: 'run', value: (r) => r.running },
    { key: 'wait', value: (r) => r.waiting },
    { key: 'size', value: (r) => r.sizeBytes },
    /* Sorts on the DURATION, not the formatted string: "~90s" and "~120s" sort
       the wrong way as text, and this column exists to find the slow loads. */
    { key: 'load', value: (r) => loadTimes[r.model]?.seconds ?? null },
  ]);

  /* $effect.pre, not $effect: it runs BEFORE the DOM is updated, so the first
     paint already has the configured cap. A plain $effect runs after, which
     would render the constructor's default once and then reflow — visible as a
     flash of the wrong number of rows on load. */
  $effect.pre(() => {
    view.pageSize = maxRows;
  });

  const shown = $derived(view.slice(rows));

  const COLUMNS: ColumnDef[] = [
    { key: 'model', label: 'model', required: true, width: 26 },
    { key: 'state', label: 'state', width: 13 },
    { key: 'node', label: 'node', width: 13 },
    { key: 'server', label: 'server:port', width: 22 },
    { key: 'tok', label: 'tok/s', right: true, width: 9 },
    /* Prefill, as its own column rather than folded into tok/s. It is what
       used to be silently added into the column to its left, and hiding it
       entirely would leave a signal collected and never shown. Switchable off
       through the column menu like any other. */
    { key: 'pre', label: 'prefill', right: true, width: 10 },
    { key: 'kv', label: 'kv', right: true, width: 8 },
    { key: 'run', label: 'run', right: true, width: 7 },
    { key: 'wait', label: 'wait', right: true, width: 8 },
    /* SIZE AND LOAD SHIPPED WITHOUT THESE TWO LINES (T1, T2) and rendered
       nothing for two days: the cell snippets, the sort values, the tooltip
       helpers and the `/api/models/loads` fetch all existed, but the rows are
       driven by this array, so a key absent here is a column that never
       reaches the DOM. The inverse of the hazard M4 called out — there the
       fear was cells and headers disagreeing about ORDER; here they disagree
       about EXISTENCE, which is quieter because nothing looks wrong.
       `tests/test_table_columns.py` now fails on either. */
    { key: 'size', label: 'size', right: true, width: 9 },
    { key: 'load', label: 'load', right: true, width: 9 },
  ];

  const cols = new ColumnView('models', COLUMNS);

  $effect(() => dropSortWhenHidden(view, (k) => cols.isVisible(k)));

  const summary = $derived.by(() => {
    const counts = new Map<ModelState, number>();
    for (const r of rows) counts.set(r.state, (counts.get(r.state) ?? 0) + 1);
    return [...counts.entries()]
      .sort((a, b) => STATE_ORDER[a[0]] - STATE_ORDER[b[0]])
      .map(([state, n]) => `${n} ${state}`)
      .join(' · ');
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

  /* ---------------------------------------------------------------- styles
   * Named class strings with their reasoning above them — see
   * `lib/styles.md`. Eleven cell branches share eight of these, so naming
   * them is forced by the table regardless of the comments.
   */

  /* Headers are quiet: small, uppercase, tracked, and muted. The data is the
     loud part of a table and a header row competing with it is noise. Relative
     positioning is the anchor for the AA resize grip, which sits on the column
     boundary rather than inside the cell's text flow. */
  /* SPLIT INTO A BASE plus the truncation, because the original CSS was three
     rules and not one: `th {...}` styled EVERY header cell, `th:not(.slack)`
     added the truncation, and `th.slack` only set a width. Collapsing that
     into one constant and giving the slack cell `w-auto` alone dropped the
     base off it -- so the header underline and every row's rule stopped short
     of the table's right edge, and the slack header picked up the UA's bold
     and centred defaults. Caught by diffing computed styles against a
     snapshot taken before the conversion, not by reading the diff. */
  const TH_BASE =
    'relative text-left text-micro font-medium tracking-[0.1em] uppercase '  +
    'text-ink-muted px-3 pt-0 pb-[6px] border-b border-rule whitespace-nowrap';

  /* `pt-0` is not decoration: with no preflight the UA's own
     `th { padding: 1px }` fills any silence the utilities leave. */
  const TH = `${TH_BASE} overflow-hidden text-ellipsis`;

  /* Body cells carry a 45%-opacity rule rather than the full one: at twelve
     rows the full weight reads as a grid, and the row separation only needs to
     be enough for the eye to track across. */
  const TD_BASE =
    'px-3 py-[5px] border-b [border-bottom-color:color-mix(in_srgb,var(--rule)_45%,transparent)] whitespace-nowrap';
  const TD = `${TD_BASE} overflow-hidden text-ellipsis`;

  /* Numbers right-aligned AND tabular, so a column of readings scans as a
     column and does not reflow as values change.

     `tabular-nums` is not cosmetic here and dropping it was a real bug during
     this conversion: `.num` is a GLOBAL helper in app.css, and replacing a
     cell's whole class attribute silently took it away. Proportional digits
     make `1.1` and `8.8` different widths, so every column shifted on every
     frame. Nothing failed; it just looked wrong.

     THE MIGRATION LESSON: converting a component drops the global classes it
     was quietly relying on. `.num` and `.dim` in app.css are the two here, and
     every component must be audited for them before its class attributes are
     rewritten. */
  const NUM = `${TD} text-right tabular-nums`;
  const DIM = `${TD} text-ink-muted`;
  const MODEL = `${TD} font-medium`;

  /* The trailing column that absorbs whatever `width: 100%` leaves over (AA1).
     Unsized on purpose: without it, fixed layout spreads the surplus across
     every column in proportion to the widths just set, undoing the point of
     setting them. Unsized is ALL it is, though -- it is an ordinary cell
     otherwise, and it carries the rule out to the table's edge. It skips only
     the clipping, having no content to clip. */
  const SLACK_TH = `${TH_BASE} w-auto`;
  const SLACK_TD = `${TD_BASE} w-auto`;

  /* A quiet marker, not a badge: it qualifies the name beside it rather than
     competing with the state column for attention. */
  const SHARDS =
    'text-micro px-1 border border-rule rounded-sm text-ink-muted whitespace-nowrap';

  /* Model lifecycle, by state. Was four `[data-state]` rules; as utilities the
     mapping has to become a lookup, so it is written as one. `unknown` and
     `loading` share warning ink deliberately — both mean "not yet answering",
     and giving them separate colours would imply a distinction the data does
     not support. */
  const STATE_TONE: Record<ModelState, string> = {
    active: 'text-good',
    sleeping: 'text-ink-2',
    loading: 'text-warning',
    unknown: 'text-warning',
    unloaded: '',
  };
</script>

{#snippet cell(c: ColumnDef, row: Row)}
  {#if c.key === 'model'}
    <td class={MODEL}>{row.model}</td>
  {:else if c.key === 'state'}
    <td>
      <span class="inline-flex items-baseline gap-[5px] {STATE_TONE[row.state]}" title={row.rawStatus}>
        <span aria-hidden="true">{MODEL_GLYPH[row.state]}</span>
        {row.state}
      </span>
    </td>
  {:else if c.key === 'size'}
    <!-- Unlike throughput, this survives a model going to sleep: a sleeping
         model still has a size, and size is what makes a load time
         interpretable — 15.6 GiB in 90s is ~175 MB/s, a disk answer rather
         than a mystery.

         An UNLOADED model shows nothing, and that is llama.cpp's answer rather
         than a gap here: it reads the GGUF header on load, so a model it has
         never loaded has no `meta` to report. Measured on the production
         router 2026-08-19 — cydonia and qwen (both sleeping) carried size,
         gemma (never loaded) carried none. Null means unknown, never zero. -->
    <td class={NUM} title={sizeDetail(row)}>
      {row.sizeBytes != null ? `${gib(row.sizeBytes)}G` : '—'}
    </td>
  {:else if c.key === 'load'}
    <!-- Approximate BY CONSTRUCTION: the duration is how many scrape samples
         caught the model mid-load, so it is a multiple of the interval with the
         interval as its error bar. Rendered with a leading ~ so it is never
         read as a measurement. Empty when this model has not loaded inside the
         window, which is not the same as loading instantly. -->
    <td class={NUM} title={loadDetail(row)}>
      {loadTimes[row.model] ? `~${Math.round(loadTimes[row.model].seconds)}s` : '—'}
    </td>
  {:else if c.key === 'node'}
    <td class={DIM}>
      {row.node}
      {#if row.shardNodes.length}
        <!-- The cluster is what SERVES the model; these are the boxes its
             shards actually sit on. Named rather than counted, because "2
             nodes" does not tell you which one to go and look at. -->
        <span class={SHARDS} title="Weights sharded across {row.shardNodes.join(', ')}"
          >{row.shardNodes.length}&times;</span>
      {/if}
    </td>
  {:else if c.key === 'server'}
    <td class={DIM}>{row.server}</td>
  {:else if c.key === 'tok'}
    <!-- Throughput and cache exist only while a model is resident. A zero would
         be indistinguishable from a loaded-but-idle model, so absent data stays
         absent. -->
    <td class={NUM}>{row.state === 'active' ? num(row.tokensPerSec, 1) : '—'}</td>
  {:else if c.key === 'pre'}
    <td class="{NUM} text-ink-muted">{row.state === 'active' ? num(row.promptTokensPerSec, 0) : '—'}</td>
  {:else if c.key === 'kv'}
    <td class={NUM}>
      {row.state === 'active' && row.kvCachePct != null ? `${num(row.kvCachePct)}%` : '—'}
    </td>
  {:else if c.key === 'run'}
    <td class={NUM}>{row.state === 'active' ? row.running : '—'}</td>
  {:else if c.key === 'wait'}
    <td class={NUM}>{row.state === 'active' ? row.waiting : '—'}</td>
  {/if}
{/snippet}

<section class="panel">
  <header>
    <h2 class="eyebrow">Models</h2>
    <span class="text-ink-muted text-label">{summary || 'none registered'}</span>
    <!-- ROW filtering, so it takes the funnel — the glyph M4 deliberately left
         unspent when it built COLUMN visibility, so the two ideas would not
         end up sharing an icon.

         Stays visible while active, like a hidden column: rows missing with no
         visible cause reads as the backend having lost them. -->
    <button
      class="idle-toggle"
      class:on={pageFocus.hideIdleModels}
      aria-pressed={pageFocus.hideIdleModels}
      title={pageFocus.hideIdleModels
        ? 'Showing loaded models only — click to include unloaded'
        : 'Hide models that hold no weights'}
      onclick={() => (pageFocus.hideIdleModels = !pageFocus.hideIdleModels)}
    >
      <span aria-hidden="true">▽</span>
      <span>loaded only</span>
    </button>
    <ColumnMenu groups={[{ view: cols }]} of="Models" />
  </header>

  {#if rows.length}
    <!-- `overflow-x-auto`, not the dead `.scroll` class this used to carry.
         Its rule went with the style block and the class stayed in the markup,
         which is silent on a wide monitor and means no horizontal scroll at
         all on a narrow one. -->
    <div class="overflow-x-auto">
      <!-- `table-fixed` is load-bearing, not styling: the colgroup widths above
           are only honoured under fixed layout, and in auto layout a dragged
           column springs back. Dropped by accident during this conversion and
           caught by tests/test_table_columns.py, which is what that guard is
           for. -->
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
              <th
                use:register={c.key}
                scope="col"
                class="{TH} {c.right ? 'text-right' : ''}"
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
          {#each shown as row (row.key)}
            <tr class:idle={row.state === 'unloaded'}>
              <!-- Same list as the headers — see ProcessTable for why that
                   matters more than it looks. -->
              {#each cols.visible() as c (c.key)}
                {@render cell(c, row)}
              {/each}
              <td class={SLACK_TD}></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    <Pager {view} total={rows.length} label="Models pages" />

  {:else}
    <p class="px-4 pt-0 pb-[14px] text-body text-ink-2">
      No models registered. Check <code>LLAMA_ROUTER_URLS</code> on the node stack.
    </p>
  {/if}
</section>

<style>
  /* THE RESIDUAL, and it is deliberate — see `lib/styles.md`. Everything that
     converted cleanly is a named constant in the script above, with its
     reasoning attached. What is left is what utilities express worse than a
     selector does. */

  /* Structural: the last row drops its rule so the table ends on data rather
     than on a line. As a variant this is `[&:last-child>td]:border-b-0` on
     every row, which is longer and says less. */
  tbody tr:last-child td {
    border-bottom: none;
  }

  /* Row hover, and it earns its place: with identity columns on the left and
     numbers on the right, the eye needs something to hold the line across the
     gap between them. On the ROW rather than the cell, which no per-cell
     utility can do. */
  tbody tr:hover {
    background: var(--panel-raised);
  }

  /* Unloaded models are context, not news — present so you know they exist,
     recessive so they don't compete with what's running. A descendant selector
     because the state is on the ROW and the colour belongs to its cells; the
     alternative threads a flag into all eleven cell branches. */
  tr.idle td {
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

  /* Matches ColumnMenu's trigger beside it: hover-revealed, persistent
     whenever it is actually filtering. Three selectors sharing one rule; as
     variants that is an opacity chain plus a conditional, for the same
     behaviour with more moving parts.

     Renamed from `.idle` — that name now belongs to the ROW state above, and
     two different meanings under one class in one file is how a stylesheet
     starts lying. */
  .idle-toggle {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font: inherit;
    font-size: var(--text-label);
    background: none;
    border: 1px solid transparent;
    border-radius: var(--radius);
    padding: 1px 6px;
    color: var(--ink-muted);
    cursor: pointer;
    opacity: 0;
  }

  header:hover .idle-toggle,
  .idle-toggle:focus-visible,
  .idle-toggle.on {
    opacity: 1;
  }

  .idle-toggle.on {
    color: var(--good);
    border-color: var(--good);
  }
</style>
