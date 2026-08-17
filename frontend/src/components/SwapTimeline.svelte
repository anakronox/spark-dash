<script lang="ts">
  /* When models loaded, slept and unloaded.
   *
   * The live view says what's loaded now. This says what happened — which is
   * the actual answer when a request felt slow twenty minutes ago, because a
   * router swap is a user-visible latency spike that leaves no trace in the
   * current state.
   *
   * Cold transitions are called out separately: a model that was evicted has
   * to read its weights back off disk, where one that merely slept still has
   * its process. That difference is seconds versus tens of seconds, and it's
   * the number worth watching if throughput feels inconsistent.
   */
  import { onMount } from 'svelte';
  import Pager from './Pager.svelte';
  import { fetchWithTimeout } from '../lib/request';
  import { TableView } from '../lib/table.svelte';
  import { compositeKey, dedupeByKey } from '../lib/keys';

  interface Props {
    /** Events before it pages. Infinity = uncapped. */
    maxRows?: number;
  }
  const { maxRows = 10 }: Props = $props();

  /* Every identifying field, not a subset. `ts + model + router` looked
     unique and isn't: the timeline is bucketed by a query step, so a model
     that went active -> sleeping -> active inside one bucket yields two events
     sharing a timestamp. A duplicate key throws and freezes the panel rather
     than dropping a row — see lib/keys.ts. */
  const eventKey = (e: Event) =>
    compositeKey(e.ts, e.node, e.router, e.model, e.from_state, e.to_state);

  interface Event {
    ts: number;
    node: string;
    router: string;
    model: string;
    from_state: string;
    to_state: string;
    label: string;
    cold: boolean;
  }

  // No theme prop: this view is plain DOM, so CSS custom properties follow the
  // theme on their own. Only canvas-based components need to be told.
  const WINDOWS = [
    { key: '1h', label: '1h', minutes: 60 },
    { key: '6h', label: '6h', minutes: 360 },
    { key: '24h', label: '24h', minutes: 1440 },
    { key: '7d', label: '7d', minutes: 10080 },
  ];

  let windowKey = $state('6h');
  let events = $state<Event[]>([]);
  let coldStarts = $state(0);
  let error = $state<string | null>(null);
  let loaded = $state(false);

  const win = $derived(WINDOWS.find((w) => w.key === windowKey) ?? WINDOWS[1]);

  /* A TableView with no columns: this is a timeline, so there is nothing to
     sort BY — the order is chronological and that is the whole point of it.
     Only the paging half is wanted, and reusing the same object keeps the page
     clamping and the range wording identical to the tables rather than
     reimplementing both slightly differently. */
  const view = new TableView<Event>([]);

  // Before paint — see ModelsTable.
  $effect.pre(() => {
    view.pageSize = maxRows;
  });

  const shown = $derived(view.slice(events));

  async function load() {
    try {
      // Step scales with the window: a 7-day view at 60s would return far more
      // points than there are transitions to find in them.
      const step = win.minutes <= 360 ? '60s' : win.minutes <= 1440 ? '120s' : '600s';
      const resp = await fetchWithTimeout(
        `/api/models/timeline?minutes=${win.minutes}&step=${step}`,
      );
      if (!resp.ok) throw new Error(String(resp.status));
      const body = await resp.json();
      events = dedupeByKey(body.events ?? [], eventKey);
      coldStarts = body.cold_starts ?? 0;
      error = null;
    } catch {
      error = 'Prometheus is unavailable, so history can\'t be read.';
      events = [];
    } finally {
      loaded = true;
    }
  }

  $effect(() => {
    void win.key;
    load();
  });

  onMount(() => {
    // Swaps happen on the scale of sleep timers (minutes), so polling faster
    // than the scrape interval would find nothing new.
    const timer = setInterval(load, 60_000);
    return () => clearInterval(timer);
  });

  function when(ts: number): string {
    const mins = Math.floor((Date.now() / 1000 - ts) / 60);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    return `${Math.floor(hours / 24)}d ago`;
  }

  function clock(ts: number): string {
    return new Date(ts * 1000).toLocaleTimeString([], {
      hour: '2-digit',
      minute: '2-digit',
    });
  }
</script>

<section class="panel">
  <header>
    <div class="titles">
      <h2 class="eyebrow">Model activity</h2>
      {#if loaded && !error}
        <span class="dim count">
          {events.length}
          {events.length === 1 ? 'change' : 'changes'}
          {#if coldStarts}
            · <span class="cold-count">{coldStarts} cold</span>
          {/if}
        </span>
      {/if}
    </div>

    <div class="windows" role="group" aria-label="Time window">
      {#each WINDOWS as w (w.key)}
        <button
          class="window"
          class:active={w.key === windowKey}
          aria-pressed={w.key === windowKey}
          onclick={() => (windowKey = w.key)}
        >
          {w.label}
        </button>
      {/each}
    </div>
  </header>

  {#if error}
    <p class="empty">{error}</p>
  {:else if loaded && !events.length}
    <p class="empty">
      No model changes in this window — every router has held its state
      throughout.
    </p>
  {:else}
    <ol class="events">
      {#each shown as e (eventKey(e))}
        <li class:cold={e.cold}>
          <span class="time" title={clock(e.ts)}>{when(e.ts)}</span>
          <span class="mark" aria-hidden="true"></span>
          <span class="body">
            <span class="model">{e.model}</span>
            <span class="label">{e.label}</span>
            {#if e.cold}
              <!-- Named rather than left to the colour: this is the part that
                   cost someone a slow request. -->
              <span class="badge">next request reloads weights</span>
            {/if}
          </span>
          <span class="where dim">{e.node} · {e.router}</span>
        </li>
      {/each}
    </ol>

    <Pager {view} total={events.length} label="Model activity pages" />
  {/if}
</section>

<style>
  section {
    padding: 14px 16px 16px;
  }

  header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 10px 16px;
    padding-bottom: 12px;
  }

  .titles {
    display: flex;
    align-items: baseline;
    gap: 14px;
  }

  .count {
    font-size: 11px;
  }

  .cold-count {
    color: var(--series-2);
  }

  .windows {
    display: flex;
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    overflow: hidden;
  }

  .window {
    font-size: 11px;
    padding: 3px 9px;
    color: var(--ink-muted);
    border-right: 1px solid var(--rule);
  }

  .window:last-child {
    border-right: none;
  }

  .window:hover {
    color: var(--ink);
  }

  .window.active {
    color: var(--ink);
    background: var(--panel-raised);
  }

  .events {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: 1px;
    /* Long windows can hold a lot of swaps; the panel shouldn't grow without
       bound and push everything else off the page. */
    max-height: 320px;
    overflow-y: auto;
  }

  li {
    display: grid;
    grid-template-columns: 72px 10px 1fr auto;
    align-items: baseline;
    gap: 10px;
    padding: 5px 0;
    font-size: 12px;
    border-bottom: 1px solid color-mix(in srgb, var(--rule) 45%, transparent);
  }

  li:last-child {
    border-bottom: none;
  }

  .time {
    color: var(--ink-muted);
    font-size: 11px;
  }

  /* A rail down the left edge, so the list reads as a sequence rather than a
     table of unrelated rows. */
  .mark {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: var(--ink-muted);
    justify-self: center;
  }

  li.cold .mark {
    background: var(--series-2);
  }

  .body {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 8px;
  }

  .model {
    font-weight: 500;
  }

  .label {
    color: var(--ink-2);
  }

  li.cold .label {
    color: var(--series-2);
  }

  .badge {
    font-size: 10px;
    color: var(--series-2);
    border: 1px solid color-mix(in srgb, var(--series-2) 40%, transparent);
    border-radius: var(--radius);
    padding: 0 5px;
  }

  .where {
    font-size: 11px;
    white-space: nowrap;
  }

  .empty {
    margin: 0;
    font-size: 12px;
    color: var(--ink-2);
  }

  @media (max-width: 640px) {
    li {
      grid-template-columns: 62px 10px 1fr;
    }
    .where {
      grid-column: 3;
      font-size: 10px;
    }
  }
</style>
