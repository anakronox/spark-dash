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
   */
  import { onMount } from 'svelte';

  interface AlertItem {
    name: string;
    severity: string;
    summary: string;
    description: string;
    node: string | null;
    started_at: string | null;
  }

  let available = $state(true);
  let alerts = $state<AlertItem[]>([]);
  let expanded = $state<string | null>(null);
  let loaded = $state(false);

  async function load() {
    try {
      const resp = await fetch('/api/alerts');
      if (!resp.ok) throw new Error(String(resp.status));
      const body = await resp.json();
      available = body.available;
      alerts = body.alerts ?? [];
    } catch {
      available = false;
      alerts = [];
    } finally {
      loaded = true;
    }
  }

  onMount(() => {
    load();
    // Alert state changes on Prometheus's evaluation interval, not the live
    // tick — polling faster would just be load with no new information.
    const timer = setInterval(load, 30_000);
    return () => clearInterval(timer);
  });

  function age(iso: string | null): string {
    if (!iso) return '';
    const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m`;
    const hours = Math.floor(mins / 60);
    return hours < 24 ? `${hours}h` : `${Math.floor(hours / 24)}d`;
  }
</script>

{#if loaded && !available}
  <p class="unavailable">
    <span aria-hidden="true">▲</span>
    Alertmanager is unreachable — nothing would notify you right now, including
    of this.
  </p>
{:else if alerts.length}
  <section class="alerts" aria-label="Firing alerts">
    {#each alerts as alert (alert.name + (alert.node ?? ''))}
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
