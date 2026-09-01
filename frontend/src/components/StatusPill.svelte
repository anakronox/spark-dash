<script lang="ts">
  /* Status as glyph + word + colour, never colour alone.
   * Colour-blind readers need the first two; so does anyone reading a
   * screenshot in a monochrome context. */
  import { HEALTH_GLYPH, HEALTH_LABEL } from '../lib/format';
  import type { HealthState } from '../lib/types';

  interface Props {
    health: HealthState;
    reasons?: string[];
    /** De-escalate the colour without changing the words. Used while the
     *  node is under a maintenance window: "critical · unreachable" is still
     *  the fact, in muted ink because it was expected. The glyph and label
     *  stay, so meaning never rode on the hue in the first place. */
    muted?: boolean;
  }
  const { health, reasons = [], muted = false }: Props = $props();

  /* SPIKE NOTE (roadmap AB). The old CSS expressed this as four
   * `[data-health='x']` rules, which read as one idea with four cases. As
   * utilities it becomes a lookup, because there is no `[data-health]`
   * selector to hang them on — the state has to become a class name.
   *
   * That is the honest trade in miniature: the mapping is now explicit and
   * greppable from the markup, and it has moved out of the stylesheet where
   * the four cases sat adjacent and obviously exhaustive. A missing case here
   * is a silent `undefined` in a class string; a missing case there was a
   * visibly unstyled pill. */
  const TONE: Record<HealthState, string> = {
    good: 'text-good',
    warning: 'text-warning',
    serious: 'text-serious',
    critical: 'text-critical',
  };
</script>

<span
  class="inline-flex items-baseline gap-[6px] text-label {muted ? 'text-ink-muted' : TONE[health]}"
  data-health={health}
  data-muted={muted ? '' : undefined}
  title={reasons.join('; ')}
>
  <span class="text-nano leading-none" aria-hidden="true">{HEALTH_GLYPH[health]}</span>
  <span>{HEALTH_LABEL[health]}</span>
  {#if reasons.length}
    <span class="text-ink-2">{reasons[0]}</span>
  {/if}
</span>
