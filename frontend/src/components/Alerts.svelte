<script lang="ts">
  /* Firing alerts.
   *
   * Sits above everything because an alert is the one thing you want to see
   * before you start reading numbers.
   *
   * "No alerts" and "can't reach Alertmanager" are rendered differently on
   * purpose. Both are quiet states, but only one of them is reassuring — a
   * dashboard that looks all-clear because its alerting is down is exactly the
   * failure this whole system exists to avoid.
   *
   * State comes from a shared AlertFeed rather than being fetched here: the
   * header trigger and the history fly-out need the same data, and three
   * independent polls could disagree with each other for 30s at a time.
   */
  import { age } from '../lib/format';
  import { alertKey } from '../lib/alerts.svelte';
  import type { AlertFeed } from '../lib/alerts.svelte';

  interface Props {
    feed: AlertFeed;
  }
  const { feed }: Props = $props();

  let expanded = $state<string | null>(null);

  const available = $derived(feed.available);
  const alerts = $derived(feed.alerts);
  const loaded = $derived(feed.loaded);
</script>

{#if loaded && !available}
  <p class="unavailable">
    <span aria-hidden="true">▲</span>
    Alertmanager is unreachable — nothing would notify you right now, including
    of this.
  </p>
{:else if alerts.length}
  <section class="alerts" aria-label="Firing alerts">
    {#each alerts as alert (alertKey(alert))}
      <button
        class="alert"
        data-severity={alert.severity}
        aria-expanded={expanded === alert.name + alert.node}
        onclick={() =>
          (expanded = expanded === alert.name + alert.node ? null : alert.name + alert.node)}
      >
        <span class="row">
          <span class="glyph" aria-hidden="true">
            {alert.severity === 'critical' ? '■' : '▲'}
          </span>
          <span class="sev">{alert.severity}</span>
          <span class="summary">{alert.summary || alert.name}</span>
          <span class="age dim">{age(alert.started_at)}</span>
        </span>
        {#if expanded === alert.name + alert.node && alert.description}
          <!-- The description says what to check, which is the part worth
               reading — kept collapsed so a firing alert stays one line. -->
          <span class="detail">{alert.description}</span>
        {/if}
      </button>
    {/each}
  </section>
{/if}

<style>
  .alerts {
    display: grid;
    gap: 6px;
  }

  .alert {
    display: block;
    width: 100%;
    text-align: left;
    padding: 8px 12px;
    border-radius: var(--radius);
    background: var(--panel);
    border: 1px solid var(--rule);
    border-left-width: 3px;
    font-size: 12px;
  }

  .row {
    display: flex;
    align-items: baseline;
    gap: 10px;
  }

  .glyph {
    font-size: 9px;
    line-height: 1;
    flex: none;
  }

  .sev {
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    flex: none;
  }

  .summary {
    color: var(--ink);
    flex: 1;
  }

  .age {
    font-size: 11px;
    flex: none;
  }

  .detail {
    display: block;
    padding: 8px 0 2px 30px;
    color: var(--ink-2);
    line-height: 1.5;
  }

  [data-severity='critical'] {
    border-left-color: var(--critical);
    color: var(--critical);
  }

  [data-severity='warning'] {
    border-left-color: var(--warning);
    color: var(--warning);
  }

  .unavailable {
    margin: 0;
    font-size: 12px;
    padding: 9px 12px;
    border-radius: var(--radius);
    background: var(--panel);
    border: 1px solid color-mix(in srgb, var(--warning) 40%, var(--rule));
    color: var(--warning);
    display: flex;
    align-items: baseline;
    gap: 8px;
  }
</style>
