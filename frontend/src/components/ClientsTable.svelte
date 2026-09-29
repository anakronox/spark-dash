<!--
  The Clients card (roadmap AM6): who called which model, how often, and how
  often it failed, from the LiteLLM gateway's metrics via Prometheus.

  Only on the page while the feature is on (AM6a), so it never renders a
  "not set up" state of its own: Settings is where that is explained.

  Built on ModelsTable's pattern -- TableView for sorting and paging,
  ColumnView for which columns show and how wide -- and its constants are
  copied rather than shared for the reason ModelsTable gives: each table's
  classes carry that table's own history.
-->
<script lang="ts">
  import { fetchWithTimeout } from '../lib/request';
  import { num } from '../lib/format';
  import Pager from './Pager.svelte';
  import ColumnMenu from './ColumnMenu.svelte';
  import ColumnGrip from './ColumnGrip.svelte';
  import SortButton from './SortButton.svelte';
  import { TableView, dropSortWhenHidden } from '../lib/table.svelte';
  import { ColumnView } from '../lib/columns.svelte';
  import type { ColumnDef } from '../lib/table.svelte';
  import { RANGES } from '../lib/history';
  import { instanceKey } from '../lib/layout.svelte';
  import { poll } from '../lib/visibility.svelte';
  import {
    clientTitle,
    countingNote,
    engineText,
    failedTitle,
    harnessTitle,
    modelText,
    rowKey,
    summaryText,
  } from '../lib/clients';
  import type { ClientRow, ClientsResponse } from '../lib/clients';

  interface Props {
    maxRows: number;
    /** Which copy of the card this is (AF), for its own saved range. */
    instance?: string;
  }
  const { maxRows, instance = 'clients' }: Props = $props();

  // svelte-ignore state_referenced_locally -- `instance` is fixed for the life of the
  // component: App keys each card by id, so a different instance is a different mount.
  const RANGE_KEY = instanceKey('spark-dash.clients-range.v1', instance);

  function readRange(): string {
    try {
      const saved = localStorage.getItem(RANGE_KEY);
      return RANGES.some((r) => r.key === saved) ? saved! : RANGES[0].key;
    } catch {
      return RANGES[0].key;
    }
  }

  let rangeKey = $state(readRange());
  const range = $derived(RANGES.find((r) => r.key === rangeKey) ?? RANGES[0]);

  function setRange(key: string) {
    rangeKey = key;
    try {
      localStorage.setItem(RANGE_KEY, key);
    } catch {
      // Still applied for this session.
    }
  }

  let rows = $state<ClientRow[]>([]);
  let countingSince = $state<number | null>(null);
  let error = $state<string | null>(null);
  let loaded = $state(false);

  async function load(minutes: number) {
    try {
      const resp = await fetchWithTimeout(`/api/clients?minutes=${minutes}`);
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(body?.detail || resp.statusText);
      const data = body as ClientsResponse;
      rows = data.rows;
      countingSince = data.counting_since;
      error = null;
    } catch (err) {
      // Keep the last rows: a Prometheus blip should not empty the table.
      error = (err as Error).message;
    } finally {
      loaded = true;
    }
  }

  /* 30 s, and only while visible (`poll`). The numbers are windowed counts
     from Prometheus, which scrapes every 15 s, so faster would redraw the
     same values; a range change reloads at once. */
  $effect(() => {
    const minutes = range.minutes;
    load(minutes);
    return poll(() => load(minutes), 30_000);
  });

  /* Default order is the backend's: most requests first. Sorting is an
     override, cycled back out of with the header, as in ModelsTable. */
  const view = new TableView<ClientRow>([
    { key: 'harness', value: (r) => r.harness.name },
    { key: 'client', value: (r) => r.client.name },
    { key: 'model', value: (r) => modelText(r) },
    { key: 'engine', value: (r) => engineText(r) },
    { key: 'requests', value: (r) => r.requests },
    { key: 'rate', value: (r) => r.per_min },
    { key: 'failed', value: (r) => r.failed },
    { key: 'active', value: (r) => (r.active ? 1 : 0) },
  ]);

  $effect.pre(() => {
    view.pageSize = maxRows;
  });

  const shown = $derived(view.slice(rows));

  /* HARNESS AND CLIENT ARE BOTH REQUIRED, and that is the finding behind the
     card rather than a layout choice. Measured 2026-09-29: the first real
     traffic was an agent sending the OpenAI SDK's default User-Agent, so the
     harness column said only "OpenAI SDK" and the machine's name was what
     identified it. Either column alone can be the one that says who. */
  const COLUMNS: ColumnDef[] = [
    { key: 'harness', label: 'harness', required: true, width: 28 },
    { key: 'client', label: 'client', required: true, width: 14 },
    { key: 'model', label: 'model', width: 26 },
    { key: 'engine', label: 'engine', width: 18 },
    { key: 'requests', label: 'requests', right: true, width: 12 },
    { key: 'rate', label: 'req/min', right: true, width: 11 },
    { key: 'failed', label: 'failed', right: true, width: 10 },
    { key: 'active', label: 'active', width: 10 },
  ];

  const cols = new ColumnView('clients', COLUMNS);

  $effect(() => dropSortWhenHidden(view, (k) => cols.isVisible(k)));

  const summary = $derived(summaryText(rows));
  const note = $derived(countingNote(countingSince));

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

  const TH_BASE =
    'relative text-left text-micro font-medium tracking-[0.1em] uppercase ' +
    'text-ink-muted px-3 pt-0 pb-[6px] border-b border-rule whitespace-nowrap';
  const TH = `${TH_BASE} overflow-hidden text-ellipsis`;
  const TD_BASE =
    'px-3 py-[var(--row-pad)] leading-[var(--row-line)] border-b [border-bottom-color:color-mix(in_srgb,var(--rule)_45%,transparent)] whitespace-nowrap';
  const TD = `${TD_BASE} overflow-hidden text-ellipsis`;
  /* `tabular-nums` spelled out: ModelsTable's note on `.num` applies here, and
     a count that changes width every poll reflows the row. */
  const NUM = `${TD} text-right tabular-nums`;
  const DIM = `${TD} text-ink-muted`;
  const NAME = `${TD} font-medium`;
  const SLACK_TH = `${TH_BASE} w-auto`;
  const SLACK_TD = `${TD_BASE} w-auto`;
  /* A library default, not a harness: said in a tag beside the name rather
     than by colour alone, so it survives a colour-blind reader. */
  const TAG = 'ml-[6px] text-micro px-1 border border-rule rounded-sm text-ink-muted';
</script>

{#snippet cell(c: ColumnDef, row: ClientRow)}
  {#if c.key === 'harness'}
    <td class={NAME} title={harnessTitle(row)}>
      <span class:text-ink-muted={row.harness.kind === 'unattributed'}>{row.harness.name}</span>
      {#if row.harness.kind === 'sdk'}<span class={TAG}>sdk</span>{/if}
    </td>
  {:else if c.key === 'client'}
    <td class={TD} title={clientTitle(row)}>{row.client.name ?? '—'}</td>
  {:else if c.key === 'model'}
    <td class="{TD} {row.rejected ? 'text-warning' : ''}" title={row.rejected ? 'Rejected at the gateway: no route matches the model it asked for.' : (row.model ?? '')}>
      {modelText(row)}
    </td>
  {:else if c.key === 'engine'}
    <td class={DIM} title={row.engine?.server ?? 'Reached no engine'}>{engineText(row)}</td>
  {:else if c.key === 'requests'}
    <td class={NUM}>{row.requests}</td>
  {:else if c.key === 'rate'}
    <td class={NUM}>{row.per_min > 0 ? num(row.per_min, 1) : '—'}</td>
  {:else if c.key === 'failed'}
    <td class="{NUM} {row.failed ? 'text-warning' : 'text-ink-muted'}" title={failedTitle(row)}>
      {row.failed || '—'}
    </td>
  {:else if c.key === 'active'}
    <!-- A glyph AND a word, like every status here: meaning never rides on
         colour alone. Active is "sent something in the last 5 minutes". -->
    <td class={TD}>
      {#if row.active}<span class="text-good"><span aria-hidden="true">●</span> now</span>{:else}<span class="text-ink-muted">—</span>{/if}
    </td>
  {/if}
{/snippet}

<section class="panel">
  <header>
    <h2 class="eyebrow">Clients</h2>
    <span class="text-ink-muted text-label">
      {summary || (loaded ? 'no requests' : '')}{#if note}{summary ? ' · ' : ''}{note}{/if}
    </span>
    <div class="tools">
      <div class="segmented" role="group" aria-label="Time range">
        {#each RANGES as r (r.key)}
          <button
            class:active={r.key === rangeKey}
            aria-pressed={r.key === rangeKey}
            onclick={() => setRange(r.key)}
          >
            {r.label}
          </button>
        {/each}
      </div>
      <ColumnMenu groups={[{ view: cols }]} of="Clients" />
    </div>
  </header>

  {#if error}
    <p class="px-4 pt-0 pb-[10px] text-label text-warning">{error}</p>
  {/if}

  {#if rows.length}
    <div class="overflow-x-auto">
      <table class="table-fixed text-body min-w-[620px]">
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
            <th class={SLACK_TH}></th>
          </tr>
        </thead>
        <tbody>
          {#each shown as row (rowKey(row))}
            <tr>
              {#each cols.visible() as c (c.key)}
                {@render cell(c, row)}
              {/each}
              <td class={SLACK_TD}></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    <Pager {view} total={rows.length} label="Clients pages" />
  {:else if loaded && !error}
    <p class="px-4 pt-0 pb-[14px] text-body text-ink-2">
      No requests through the gateway in the last {range.label}. A client that still calls an engine
      directly does not appear here.
    </p>
  {/if}
</section>

<style>
  /* The residual, as in ModelsTable: what a selector says better than a
     utility does. */
  tbody tr:last-child td {
    border-bottom: none;
  }

  tbody tr:hover {
    background: var(--panel-raised);
  }

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

  .tools {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-left: auto;
  }
</style>
