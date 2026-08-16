<script lang="ts">
  /* Alert history, as a right-anchored fly-out.
   *
   * WHY A FLY-OUT. Not a tab: history is low-frequency reference data, and a
   * top-level tab implies parity with the live dashboard while putting the
   * live view behind a click. Not an inline disclosure either — the alerts
   * region sits above everything, so expanding it reflows the whole page every
   * time you glance at history. An overlay leaves the layout beneath untouched
   * and has room for a real list.
   *
   * THE BANNER IS NOT REPLACED BY THIS. Firing alerts must stay visible with
   * no interaction at all; if this were the only route to discovering
   * something is wrong, that would be a regression for a monitoring dashboard.
   * This is purely additive.
   *
   * Built on <dialog> + showModal() so focus trapping, Escape, the backdrop
   * and focus restore come from the platform rather than being hand-rolled.
   */
  import { ageFromEpoch, duration } from '../lib/format';
  import { HISTORY_RANGES, fetchHistory } from '../lib/alerts.svelte';
  import type { AlertEpisode, AlertFeed, AlertSummary } from '../lib/alerts.svelte';

  interface Props {
    feed: AlertFeed;
    open: boolean;
    onclose: () => void;
  }
  const { feed, open, onclose }: Props = $props();

  let dialog = $state<HTMLDialogElement | null>(null);
  let range = $state<number>(HISTORY_RANGES[1].minutes);
  let episodes = $state<AlertEpisode[]>([]);
  let summary = $state<AlertSummary | null>(null);
  let loading = $state(false);
  let failed = $state(false);

  let controller: AbortController | null = null;

  /* Fetched on open and on range change ONLY. A fly-out re-running a
   * query_range every 30s while it happens to be open is pure waste — the
   * history it shows is minutes old by nature. Current alerts underneath keep
   * their own 30s cadence via the shared feed. */
  async function load() {
    controller?.abort();
    controller = new AbortController();
    loading = true;
    failed = false;
    try {
      const body = await fetchHistory(range, controller.signal);
      episodes = body.episodes;
      summary = body.summary;
    } catch (err) {
      if ((err as Error)?.name === 'AbortError') return;
      failed = true;
      episodes = [];
      summary = null;
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    if (!dialog) return;
    if (open && !dialog.open) {
      dialog.showModal();
      load();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  });

  function pick(minutes: number) {
    range = minutes;
    load();
  }

  const rangeLabel = $derived(
    HISTORY_RANGES.find((r) => r.minutes === range)?.label ?? '',
  );
</script>

<dialog
  bind:this={dialog}
  class="flyout"
  aria-label="Alerts and history"
  onclose={onclose}
  onclick={(e) => {
    // Clicking the backdrop closes. The dialog element itself is the backdrop's
    // hit target, so a click landing on it rather than on the panel is outside.
    if (e.target === dialog) onclose();
  }}
>
  <div class="panel">
    <header>
      <h2 class="eyebrow">Alerts</h2>
      <button class="close" aria-label="Close" onclick={onclose}>×</button>
    </header>

    <!-- Now -->
    <section class="block">
      <h3 class="eyebrow dim">Firing now</h3>
      {#if !feed.available}
        <p class="note" data-tone="warning">
          Alertmanager is unreachable — nothing would notify you right now.
        </p>
      {:else if feed.alerts.length === 0}
        <p class="note dim">Nothing firing.</p>
      {:else}
        {#each feed.alerts as a (a.name + (a.node ?? ''))}
          <div class="row now" data-severity={a.severity}>
            <span class="glyph" aria-hidden="true">{a.severity === 'critical' ? '■' : '▲'}</span>
            <span class="name">{a.summary || a.name}</span>
            <span class="dim node">{a.node ?? ''}</span>
          </div>
        {/each}
      {/if}
    </section>

    <!-- History -->
    <section class="block">
      <div class="block-head">
        <h3 class="eyebrow dim">History</h3>
        <div class="ranges" role="group" aria-label="History range">
          {#each HISTORY_RANGES as r (r.key)}
            <button
              class="range"
              class:active={range === r.minutes}
              aria-pressed={range === r.minutes}
              onclick={() => pick(r.minutes)}
            >{r.label}</button>
          {/each}
        </div>
      </div>

      {#if loading}
        <p class="note dim">Loading…</p>
      {:else if failed}
        <p class="note" data-tone="warning">
          Couldn't read history — Prometheus may be unreachable.
        </p>
      {:else if summary}
        <!-- The summary line leads with pending_only on purpose. An alert that
             keeps going pending and never fires means its RULE is mistuned
             rather than its condition being rare, and that state is invisible
             everywhere else, including in Alertmanager. -->
        <p class="summary">
          <span class="num">{summary.fired}</span> fired ·
          <span class="num">{summary.pending_only}</span> pending only ·
          <span class="dim">{summary.episodes} total in {rangeLabel}</span>
        </p>

        {#if episodes.length === 0}
          <p class="note dim">No alert has been pending or firing in {rangeLabel}.</p>
        {:else}
          <div class="scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">alert</th>
                  <th scope="col">node</th>
                  <th scope="col">when</th>
                  <th scope="col" class="r">lasted</th>
                  <th scope="col">outcome</th>
                </tr>
              </thead>
              <tbody>
                {#each episodes as e (e.alertname + e.node + e.started_at)}
                  <tr data-severity={e.severity}>
                    <td class="name">{e.alertname}</td>
                    <td class="dim">{e.node ?? '—'}</td>
                    <td class="dim num">{ageFromEpoch(e.started_at)} ago</td>
                    <td class="r num">{duration(e.duration_s)}</td>
                    <td>
                      {#if e.ongoing}
                        <span class="tag" data-kind="ongoing">ongoing</span>
                      {:else if e.fired}
                        <span class="tag" data-kind="fired">fired</span>
                      {:else}
                        <!-- Not a lesser event: it means the condition was met
                             but never for long enough to page anyone. -->
                        <span class="tag" data-kind="pending">never fired</span>
                      {/if}
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      {/if}
    </section>
  </div>
</dialog>

<style>
  .flyout {
    /* Right-anchored and full height, so it reads as a panel sliding in rather
       than a centred modal interrupting the page. */
    margin: 0 0 0 auto;
    height: 100%;
    max-height: 100%;
    width: min(680px, 100%);
    max-width: 100%;
    padding: 0;
    border: none;
    border-left: 1px solid var(--rule);
    background: var(--panel);
    color: var(--ink);
  }

  .flyout::backdrop {
    background: rgba(0, 0, 0, 0.45);
  }

  .panel {
    display: flex;
    flex-direction: column;
    gap: 18px;
    height: 100%;
    overflow-y: auto;
    padding: 18px 20px 28px;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    background: var(--panel);
    padding-bottom: 8px;
  }

  .close {
    font-size: 20px;
    line-height: 1;
    padding: 2px 8px;
    border-radius: var(--radius);
    color: var(--ink-muted);
  }
  .close:hover { color: var(--ink); }

  .block { display: flex; flex-direction: column; gap: 8px; }

  .block-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
  }

  .ranges { display: flex; gap: 4px; }

  .range {
    font-size: 11px;
    padding: 2px 8px;
    border-radius: var(--radius);
    color: var(--ink-muted);
  }
  .range.active { color: var(--ink); background: var(--rule); }

  .note { font-size: 12px; margin: 0; }
  .note[data-tone='warning'] { color: var(--warning, var(--ink)); }

  .summary { font-size: 12px; margin: 0 0 4px; }

  .row.now {
    display: flex;
    align-items: baseline;
    gap: 8px;
    font-size: 12px;
    padding: 6px 8px;
    border-radius: var(--radius);
    border: 1px solid var(--rule);
  }

  .row.now[data-severity='critical'] .glyph { color: var(--critical, var(--ink)); }

  .name { font-weight: 500; }
  .node { margin-left: auto; }

  .scroll { overflow-x: auto; }

  table { width: 100%; border-collapse: collapse; font-size: 12px; }

  th {
    text-align: left;
    font-weight: 500;
    font-size: 10px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--ink-muted);
    padding: 4px 8px;
    border-bottom: 1px solid var(--rule);
  }

  td { padding: 5px 8px; border-bottom: 1px solid var(--rule); }
  tbody tr:last-child td { border-bottom: none; }

  .r { text-align: right; }
  .num { font-variant-numeric: tabular-nums; }

  .tag {
    font-size: 10px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 1px 6px;
    border-radius: var(--radius);
    border: 1px solid var(--rule);
    color: var(--ink-muted);
  }
  .tag[data-kind='fired'] { color: var(--series-2, var(--ink)); }
  .tag[data-kind='ongoing'] { color: var(--series-1, var(--ink)); }

  @media (max-width: 640px) {
    .flyout { width: 100%; border-left: none; }
  }

  /* The slide is decoration; honour a request not to see it. */
  @media (prefers-reduced-motion: no-preference) {
    .flyout[open] { animation: slide-in 160ms ease-out; }
  }

  @keyframes slide-in {
    from { transform: translateX(12px); opacity: 0; }
    to { transform: translateX(0); opacity: 1; }
  }
</style>
