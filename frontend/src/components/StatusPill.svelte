<script lang="ts">
  /* Status as glyph + word + colour, never colour alone.
   * Colour-blind readers need the first two; so does anyone reading a
   * screenshot in a monochrome context. */
  import { HEALTH_GLYPH, HEALTH_LABEL } from '../lib/format';
  import type { HealthState } from '../lib/types';

  interface Props {
    health: HealthState;
    reasons?: string[];
  }
  const { health, reasons = [] }: Props = $props();
</script>

<span class="pill" data-health={health} title={reasons.join('; ')}>
  <span class="glyph" aria-hidden="true">{HEALTH_GLYPH[health]}</span>
  <span>{HEALTH_LABEL[health]}</span>
  {#if reasons.length}
    <span class="reason">{reasons[0]}</span>
  {/if}
</span>

<style>
  .pill {
    display: inline-flex;
    align-items: baseline;
    gap: 6px;
    font-size: 11px;
  }
  .glyph {
    font-size: 9px;
    line-height: 1;
  }
  .reason {
    color: var(--ink-2);
  }
  [data-health='good'] {
    color: var(--good);
  }
  [data-health='warning'] {
    color: var(--warning);
  }
  [data-health='serious'] {
    color: var(--serious);
  }
  [data-health='critical'] {
    color: var(--critical);
  }
</style>
