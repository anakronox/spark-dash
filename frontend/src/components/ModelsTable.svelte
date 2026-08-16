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
  import { MODEL_GLYPH, num } from '../lib/format';
  import type { NodeSnapshot, ModelState } from '../lib/types';

  interface Props {
    nodes: NodeSnapshot[];
  }
  const { nodes }: Props = $props();

  interface Row {
    key: string;
    node: string;
    runtime: string;
    server: string;
    model: string;
    state: ModelState;
    rawStatus: string;
    tokensPerSec: number | null;
    kvCachePct: number | null;
    running: number;
    waiting: number;
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
            tokensPerSec: m.tokens_per_sec,
            kvCachePct: m.kv_cache_pct,
            running: m.requests_running,
            waiting: m.requests_waiting,
          });
        }
      }
      for (const v of node.runtimes.vllm) {
        out.push({
          key: `${node.node_id}/vllm/${v.model}`,
          node: node.node_id,
          runtime: 'vllm',
          // vLLM has no router in front of it, so its own endpoint is where
          // the model is served from. Showing a dash here made it look like
          // missing data rather than a different topology.
          server: v.server || '—',
          model: v.model,
          state: 'active',
          rawStatus: '',
          tokensPerSec: v.tokens_per_sec,
          kvCachePct: v.kv_cache_pct,
          running: v.requests_running,
          waiting: v.requests_waiting,
        });
      }
    }
    // Active first: what's serving right now is what you came to see.
    return out.sort(
      (a, b) => STATE_ORDER[a.state] - STATE_ORDER[b.state] || a.model.localeCompare(b.model),
    );
  });

  const summary = $derived.by(() => {
    const counts = new Map<ModelState, number>();
    for (const r of rows) counts.set(r.state, (counts.get(r.state) ?? 0) + 1);
    return [...counts.entries()]
      .sort((a, b) => STATE_ORDER[a[0]] - STATE_ORDER[b[0]])
      .map(([state, n]) => `${n} ${state}`)
      .join(' · ');
  });
</script>

<section class="panel">
  <header>
    <h2 class="eyebrow">Models</h2>
    <span class="dim count">{summary || 'none registered'}</span>
  </header>

  {#if rows.length}
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th scope="col">model</th>
            <th scope="col">state</th>
            <th scope="col">node</th>
            <th scope="col">server:port</th>
            <th scope="col" class="r">tok/s</th>
            <th scope="col" class="r">kv</th>
            <th scope="col" class="r">run</th>
            <th scope="col" class="r">wait</th>
          </tr>
        </thead>
        <tbody>
          {#each rows as row (row.key)}
            <tr class:idle={row.state === 'unloaded'}>
              <td class="model">{row.model}</td>
              <td>
                <span class="state" data-state={row.state} title={row.rawStatus}>
                  <span aria-hidden="true">{MODEL_GLYPH[row.state]}</span>
                  {row.state}
                </span>
              </td>
              <td class="dim">{row.node}</td>
              <td class="dim">{row.server}</td>
              <!-- Throughput and cache exist only while a model is resident.
                   A zero would be indistinguishable from a loaded-but-idle
                   model, so absent data stays absent. -->
              <td class="r num">{row.state === 'active' ? num(row.tokensPerSec, 1) : '—'}</td>
              <td class="r num">
                {row.state === 'active' && row.kvCachePct != null
                  ? `${num(row.kvCachePct)}%`
                  : '—'}
              </td>
              <td class="r num">{row.state === 'active' ? row.running : '—'}</td>
              <td class="r num">{row.state === 'active' ? row.waiting : '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
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

  .r {
    text-align: right;
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
