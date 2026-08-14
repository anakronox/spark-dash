<script lang="ts">
  /* Whether what you're looking at is actually current.
   *
   * The most dangerous failure in a monitoring UI is not an error — it's a
   * page that keeps rendering the last frame it received. You'd read it,
   * believe it, and act on numbers from ten minutes ago. So liveness is
   * displayed continuously rather than only when something breaks, and the
   * heartbeat is driven by real frames rather than a CSS loop: a pulse that
   * animates on a timer would keep pulsing after the data stopped.
   */
  import type { ConnectionState } from '../lib/live.svelte';

  interface Props {
    state: ConnectionState;
    tick: number;
    secondsSinceFrame: number;
  }
  const { state, tick, secondsSinceFrame }: Props = $props();

  const label = $derived(
    state === 'live'
      ? `live · ${secondsSinceFrame}s`
      : state === 'connecting'
        ? 'connecting'
        : state === 'reconnecting'
          ? `reconnecting · last frame ${secondsSinceFrame}s ago`
          : 'offline',
  );
</script>

<div class="conn" data-state={state} role="status" aria-live="polite">
  <!-- Keyed on tick so the pulse restarts per frame: it stops when data stops,
       which is the entire point. -->
  {#key tick}
    <span class="dot" data-state={state} aria-hidden="true"></span>
  {/key}
  <span>{label}</span>
</div>

<style>
  .conn {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    font-size: 11px;
    color: var(--ink-2);
  }

  .dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    flex: none;
    background: var(--ink-muted);
  }

  .dot[data-state='live'] {
    background: var(--good);
    animation: beat 900ms ease-out;
  }

  .dot[data-state='reconnecting'] {
    background: var(--warning);
  }

  .dot[data-state='offline'] {
    background: var(--critical);
  }

  [data-state='reconnecting'] {
    color: var(--warning);
  }

  [data-state='offline'] {
    color: var(--critical);
  }

  @keyframes beat {
    from {
      box-shadow: 0 0 0 0 color-mix(in srgb, var(--good) 70%, transparent);
    }
    to {
      box-shadow: 0 0 0 7px transparent;
    }
  }
</style>
