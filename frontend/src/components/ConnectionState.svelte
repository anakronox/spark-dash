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

  /* SPIKE NOTE (roadmap AB). THIS IS THE COMPONENT THAT DOES NOT FULLY
   * CONVERT, and it is the useful one to have tried.
   *
   * The `beat` keyframe uses `color-mix(in srgb, var(--good) 70%, transparent)`
   * so the pulse takes the current theme's green. Tailwind v4 can register an
   * animation via `--animate-*` in `@theme`, but the keyframes themselves are
   * still hand-written CSS that has to live somewhere — either in app.css,
   * where a component-specific animation does not belong, or in a scoped
   * style block here, which is what this does. (Writing the literal tag name
   * in this comment broke the parser once: Svelte reads it as a real tag even
   * inside a script comment.)
   *
   * So the honest result for a component with a bespoke animation is HYBRID:
   * utilities for layout and colour, a residual style block for the one thing
   * utilities cannot express. That is not a failure of the conversion, it is
   * what the conversion looks like — and a migration should expect it rather
   * than treat each case as a defeat.
   */
  const TONE: Record<ConnectionState, string> = {
    live: 'text-ink-2',
    connecting: 'text-ink-2',
    reconnecting: 'text-warning',
    offline: 'text-critical',
  };
  const DOT: Record<ConnectionState, string> = {
    live: 'bg-good beat',
    connecting: 'bg-ink-muted',
    reconnecting: 'bg-warning',
    offline: 'bg-critical',
  };
</script>

<div
  class="inline-flex items-center gap-[7px] text-label {TONE[state]}"
  role="status"
  aria-live="polite"
>
  <!-- Keyed on tick so the pulse restarts per frame: it stops when data stops,
       which is the entire point. -->
  {#key tick}
    <span class="size-[7px] flex-none rounded-full {DOT[state]}" aria-hidden="true"></span>
  {/key}
  <span>{label}</span>
</div>

<style>
  /* The residual: a theme-aware keyframe, which has no utility form. */
  .beat {
    animation: beat 900ms ease-out;
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
