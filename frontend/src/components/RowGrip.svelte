<script lang="ts">
  /* Drag handle for the plot height of one chart card.
   *
   * The horizontal twin of `ColumnGrip`, and deliberately the same contract:
   * a focusable `separator` with a value the arrow keys change, a 16px step
   * that shift makes coarse, and Home/Escape/double-click as the way back to
   * the default. Two resize gestures on one page that behaved differently
   * would be two things to learn instead of one.
   *
   * THE HAZARD THIS IS SHAPED AROUND is the card underneath rather than a
   * button inside it. `Section` starts a MOVE from its own handle, and the
   * band's drop targeting reads the pointer across the whole page while that
   * move is live — so a resize that let its pointerdown through would risk
   * being read as the start of a card drag, and the card would fly off toward
   * a zone while the reader thought they were making a chart taller. Stopping
   * the event here means the move gesture never begins, which is the same
   * reasoning ColumnGrip applies to the sort button it sits on top of.
   *
   * It sits at the BOTTOM edge of the plot grid, in the card's own padding,
   * which is the edge the gesture is about — you drag the bottom of the charts
   * down to make them taller. The section handle lives in the left gutter at
   * the top, so the two never share pixels.
   */
  import { MIN_PLOT_PX, MAX_PLOT_PX } from '../lib/layout.svelte';

  interface Props {
    /** Current plot height in px. */
    height: () => number;
    onresize: (px: number) => void;
    /** Back to the default height. */
    onreset: () => void;
    /** What is being resized, for the accessible name. */
    label: string;
    /** How many plots the drag moves at once. A grid of eight charts grows by
     *  eight times what the pointer travelled, so the number belongs in the
     *  label — otherwise the card leaps and the control looks broken. */
    plots: number;
  }
  const { height, onresize, onreset, label, plots }: Props = $props();

  let dragging = $state(false);

  /** Same 16px as ColumnGrip: a visible step without being a jump. */
  const STEP = 16;

  function onpointerdown(e: PointerEvent) {
    // Left button only — a right-click here should open a context menu.
    if (e.button !== 0) return;
    // The whole point: the card move underneath must never see the gesture.
    e.preventDefault();
    e.stopPropagation();

    const startY = e.clientY;
    const startH = height();
    dragging = true;

    const move = (ev: PointerEvent) => onresize(startH + (ev.clientY - startY));
    const up = () => {
      dragging = false;
      globalThis.removeEventListener('pointermove', move);
      globalThis.removeEventListener('pointerup', up);
      globalThis.removeEventListener('pointercancel', up);
    };
    globalThis.addEventListener('pointermove', move);
    globalThis.addEventListener('pointerup', up);
    globalThis.addEventListener('pointercancel', up);
  }

  function onkeydown(e: KeyboardEvent) {
    const step = e.shiftKey ? STEP * 4 : STEP;
    if (e.key === 'ArrowUp') onresize(height() - step);
    else if (e.key === 'ArrowDown') onresize(height() + step);
    else if (e.key === 'Home' || e.key === 'Escape') onreset();
    else return;
    e.preventDefault();
    /* Stopped as well as prevented: `Section` listens for Escape on the WINDOW
       to abandon a card move, and ArrowUp/ArrowDown on its handle move the
       card between siblings. Neither should fire from in here. */
    e.stopPropagation();
  }
</script>

<!-- Same two suppressions, same reason as ColumnGrip: a focusable `separator`
     IS the ARIA window-splitter pattern, and the linter only knows the
     decorative variant. -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div
  class="grip"
  class:dragging
  role="separator"
  aria-orientation="horizontal"
  aria-label={plots === 1
    ? `Resize ${label} chart height`
    : `Resize ${label} chart height. Applies to all ${plots} charts.`}
  aria-valuenow={Math.round(height())}
  aria-valuemin={MIN_PLOT_PX}
  aria-valuemax={MAX_PLOT_PX}
  title="Drag to resize {label} charts, or focus and use arrow keys. Double-click to reset."
  tabindex="0"
  {onpointerdown}
  {onkeydown}
  ondblclick={(e) => {
    e.stopPropagation();
    onreset();
  }}
></div>

<style>
  /* 10px of grab area for a 2px cue — the same trade ColumnGrip makes, and the
     same reason: the target has to be findable with a pointer that is only
     roughly aimed, while the thing you SEE has to be thin enough not to read
     as a rule between sections. */
  .grip {
    position: relative;
    height: 10px;
    margin-top: 2px;
    cursor: row-resize;
    touch-action: none;
  }

  .grip::after {
    content: '';
    position: absolute;
    /* A FIXED 48px centred, not a percentage. At 40% inset either side the
       cue was 20% of the card — around 290px on a full-width one — which
       reads as a rule between sections rather than as something to grab, and
       it changed size with the card, so the same control looked like two
       different ones on a half and a full. ColumnGrip's cue is a fixed 2px for
       the same reason. */
    left: 50%;
    top: 4px;
    width: 48px;
    margin-left: -24px;
    height: 2px;
    border-radius: 1px;
    background: var(--rule);
    opacity: 0;
    transition: opacity 0.12s ease;
  }

  .grip:hover::after,
  .grip:focus-visible::after,
  .grip.dragging::after {
    opacity: 1;
    background: var(--good);
  }

  .grip:focus-visible {
    outline: none;
  }

  @media (prefers-reduced-motion: reduce) {
    .grip::after {
      transition: none;
    }
  }

  /* Touch has no hover, so leave the cue faintly on and make the strip a
     fingertip rather than a pointer tip. */
  @media (pointer: coarse) {
    .grip {
      height: 16px;
    }

    .grip::after {
      opacity: 0.5;
      top: 7px;
    }
  }
</style>
