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
  import SortButton from './SortButton.svelte';
  import { TableView, dropSortWhenHidden } from '../lib/table.svelte';
  import { ColumnView } from '../lib/columns.svelte';
  import type { ColumnDef } from '../lib/table.svelte';
  import type { NodeSnapshot, ModelState } from '../lib/types';
  import { engines } from '../lib/types';

  interface Props {
    nodes: NodeSnapshot[];
    /** Rows before it pages. Infinity = uncapped. */
    maxRows?: number;
  }
  const { nodes, maxRows = 10 }: Props = $props();

  interface Row {
    key: string;
    node: string;
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
    const out: Row[] = [];
    for (const node of nodes) {
      for (const router of node.runtimes.llama_cpp) {
        for (const m of router.models) {
          out.push({
            key: `${node.node_id}/${router.endpoint}/${m.name}`,
            node: node.node_id,
            runtime: 'llama.cpp',
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
            key: `${node.node_id}/${runtime}/${v.model}`,
            node: node.node_id,
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
            // Neither engine exposes an equivalent of llama.cpp's `meta`.
            sizeBytes: null,
            nParams: null,
            quantization: null,
            contextLength: null,
          });
        }
      }
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
    { key: 'model', label: 'model', required: true },
    { key: 'state', label: 'state' },
    { key: 'node', label: 'node' },
    { key: 'server', label: 'server:port' },
    { key: 'tok', label: 'tok/s', right: true },
    /* Prefill, as its own column rather than folded into tok/s. It is what
       used to be silently added into the column to its left, and hiding it
       entirely would leave a signal collected and never shown. Switchable off
       through the column menu like any other. */
    { key: 'pre', label: 'prefill', right: true },
    { key: 'kv', label: 'kv', right: true },
    { key: 'run', label: 'run', right: true },
    { key: 'wait', label: 'wait', right: true },
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
</script>

{#snippet cell(c: ColumnDef, row: Row)}
  {#if c.key === 'model'}
    <td class="model">{row.model}</td>
  {:else if c.key === 'state'}
    <td>
      <span class="state" data-state={row.state} title={row.rawStatus}>
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
    <td class="r num size" title={sizeDetail(row)}>
      {row.sizeBytes != null ? `${gib(row.sizeBytes)}G` : '—'}
    </td>
  {:else if c.key === 'load'}
    <!-- Approximate BY CONSTRUCTION: the duration is how many scrape samples
         caught the model mid-load, so it is a multiple of the interval with the
         interval as its error bar. Rendered with a leading ~ so it is never
         read as a measurement. Empty when this model has not loaded inside the
         window, which is not the same as loading instantly. -->
    <td class="r num load" title={loadDetail(row)}>
      {loadTimes[row.model] ? `~${Math.round(loadTimes[row.model].seconds)}s` : '—'}
    </td>
  {:else if c.key === 'node'}
    <td class="dim">{row.node}</td>
  {:else if c.key === 'server'}
    <td class="dim">{row.server}</td>
  {:else if c.key === 'tok'}
    <!-- Throughput and cache exist only while a model is resident. A zero would
         be indistinguishable from a loaded-but-idle model, so absent data stays
         absent. -->
    <td class="r num toks">{row.state === 'active' ? num(row.tokensPerSec, 1) : '—'}</td>
  {:else if c.key === 'pre'}
    <td class="r num toks dim">{row.state === 'active' ? num(row.promptTokensPerSec, 0) : '—'}</td>
  {:else if c.key === 'kv'}
    <td class="r num pct">
      {row.state === 'active' && row.kvCachePct != null ? `${num(row.kvCachePct)}%` : '—'}
    </td>
  {:else if c.key === 'run'}
    <td class="r num queue">{row.state === 'active' ? row.running : '—'}</td>
  {:else if c.key === 'wait'}
    <td class="r num queue">{row.state === 'active' ? row.waiting : '—'}</td>
  {/if}
{/snippet}

<section class="panel">
  <header>
    <h2 class="eyebrow">Models</h2>
    <span class="dim count">{summary || 'none registered'}</span>
    <ColumnMenu groups={[{ view: cols }]} of="Models" />
  </header>

  {#if rows.length}
    <div class="scroll">
      <table>
        <thead>
          <tr>
            {#each cols.visible() as c (c.key)}
              <th scope="col" class:r={c.right} aria-sort={view.ariaSort(c.key)}>
                <SortButton {view} id={c.key} label={c.label} />
              </th>
            {/each}
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
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    <Pager {view} total={rows.length} label="Models pages" />

  {:else}
    <p class="empty">
      No models registered. Check <code>LLAMA_ROUTER_URLS</code> on the node stack.
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

  /* Unloaded models are context, not news — present so you know they exist,
     recessive so they don't compete with what's running. */
  tr.idle td {
    color: var(--ink-muted);
  }

  .model {
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

  /* Reserved widths — see NetworkPanel. These columns are the most volatile on
     the page: they swing between an em dash and a live reading every time a
     model wakes or sleeps, which is a width change on every transition. */
  .toks {
    min-width: calc(7ch + 24px);
  }

  .pct {
    min-width: calc(5ch + 24px);
  }

  .queue {
    min-width: calc(4ch + 24px);
  }

  .state {
    display: inline-flex;
    align-items: baseline;
    gap: 5px;
  }

  [data-state='active'] {
    color: var(--good);
  }
  [data-state='sleeping'] {
    color: var(--ink-2);
  }
  [data-state='loading'] {
    color: var(--warning);
  }
  [data-state='unknown'] {
    color: var(--warning);
  }

  .empty {
    padding: 0 16px 14px;
    font-size: 12px;
    color: var(--ink-2);
  }

  code {
    color: var(--ink);
  }
</style>
