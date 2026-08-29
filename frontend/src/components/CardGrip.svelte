<script lang="ts">
  /* Resize corner for one card, bottom-right.
   *
   * THE CORNER IS THE POINT. The first version of this put a 48px bar at the
   * bottom CENTRE of the chart grid, hidden until hover, and on the two chart
   * cards only. It was not findable: the first person to use it in packed mode
   * went looking for a window-style resize corner, which is where a resize
   * control lives on every other surface they use. So it is there now, on every
   * card, and it stays faintly visible at rest rather than appearing on hover.
   *
   * That faint always-on state is a deliberate exception to how this page
   * treats card controls. The move handle and the fold chevron are both
   * invisible until you hover the card, because a permanent grip on every panel
   * is noise on something meant to read as an instrument. A resize corner is
   * the one that cannot afford it — hover-to-reveal only works if you already
   * know to hover, and the failure mode is a control nobody finds.
   *
   * THE GESTURE IS VERTICAL ONLY, so the cursor is `ns-resize` and not the
   * `se-resize` the corner shape suggests. Width is a separate, unbuilt thing
   * (roadmap AE4) with its own constraint — column widths must stay an fr
   * RATIO, never a pixel — and a cursor promising a horizontal drag that does
   * nothing would be worse than a corner that only goes one way.
   *
   * WHAT IT RESIZES IS THE CALLER'S PROBLEM. This reports a pixel delta from
   * where the drag started; `Section` decides whether that means plot height or
   * a row count, because only the card knows which of those it is currently
   * showing. Same reason the aria values are passed in rather than derived.
   *
   * THE HAZARD, inherited from `ColumnGrip` and sharper here: the thing
   * underneath is the card's own drag-to-move handle and the band's drop
   * targeting, so a gesture that leaked would rearrange the page rather than
   * merely re-sort a table. pointerdown stops; so does keydown, which
   * ColumnGrip never needed — `Section` abandons a move on window Escape and
   * moves the card on ArrowUp/ArrowDown, so both of this grip's bindings
   * collide with one of its neighbour's.
   */

  interface Props {
    /** Called once as the drag begins, so the caller can snapshot its state. */
    onstart: () => void;
    /** Pixels the pointer has travelled since `onstart`. Positive is down. */
    onmove: (dy: number) => void;
    /** Keyboard equivalent. `coarse` is the shift key. */
    onstep: (dir: -1 | 1, coarse: boolean) => void;
    /** Back to the card's default height. */
    onreset: () => void;
    /** Accessible name and current value, both owned by the caller since only
     *  it knows whether this card is measured in pixels or in rows. */
    label: string;
    valuenow: number;
    valuemin: number;
    valuemax: number;
    /** Spoken instead of the bare number, e.g. "13 rows" or "220 pixels". */
    valuetext: string;
  }
  const {
    onstart,
    onmove,
    onstep,
    onreset,
    label,
    valuenow,
    valuemin,
    valuemax,
    valuetext,
  }: Props = $props();

  let dragging = $state(false);

  function onpointerdown(e: PointerEvent) {
    // Left button only — a right-click here should open a context menu.
    if (e.button !== 0) return;
    // The whole point: the card's move gesture must never see this.
    e.preventDefault();
    e.stopPropagation();

    const startY = e.clientY;
    onstart();
    dragging = true;

    const move = (ev: PointerEvent) => onmove(ev.clientY - startY);
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
    if (e.key === 'ArrowUp') onstep(-1, e.shiftKey);
    else if (e.key === 'ArrowDown') onstep(1, e.shiftKey);
    else if (e.key === 'Home' || e.key === 'Escape') onreset();
    else return;
    e.preventDefault();
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
  aria-label="Resize {label}"
  aria-valuenow={valuenow}
  aria-valuemin={valuemin}
  aria-valuemax={valuemax}
  aria-valuetext={valuetext}
  title="Drag to resize {label} ({valuetext}), or focus and use arrow keys. Double-click to reset."
  tabindex="0"
  {onpointerdown}
  {onkeydown}
  ondblclick={(e) => {
    e.stopPropagation();
    onreset();
  }}
>
  <!-- Three stepped ticks: the corner-grip convention, and it reads as "drag
       me" at 10px where a single line reads as a border artefact. -->
  <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
    <path d="M11 5 L5 11 M11 8.5 L8.5 11 M11 1.5 L1.5 11" />
  </svg>
</div>

<style>
  /* 18px of target for a 12px mark. Bigger than the cue for the same reason
     ColumnGrip gives 8px to a 2px line: the thing you aim at has to forgive a
     roughly-aimed pointer, and the thing you SEE has to stay quiet. */
  .grip {
    position: absolute;
    right: 2px;
    bottom: 2px;
    z-index: 2;
    display: grid;
    place-items: center;
    width: 18px;
    height: 18px;
    cursor: ns-resize;
    color: var(--ink-muted);
    /* Faint, not absent. See the header — this is the one card control that
       cannot be hover-to-reveal. */
    opacity: 0.25;
    transition: opacity 0.12s ease, color 0.12s ease;
    touch-action: none;
  }

  .grip svg {
    display: block;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.2;
    stroke-linecap: round;
  }

  /* Hovering the CARD brings it up, the same as the move handle and the fold
     chevron — so all three card controls answer to one gesture even though
     this one starts from a different resting state. */
  :global(.slot:hover) .grip,
  .grip:hover,
  .grip:focus-visible,
  .grip.dragging {
    opacity: 1;
  }

  .grip:hover,
  .grip:focus-visible,
  .grip.dragging {
    color: var(--good);
  }

  .grip:focus-visible {
    outline: none;
  }

  @media (prefers-reduced-motion: reduce) {
    .grip {
      transition: none;
    }
  }

  /* Touch has no hover, so it stays up and gets a fingertip-sized target. */
  @media (pointer: coarse) {
    .grip {
      width: 28px;
      height: 28px;
      opacity: 0.5;
    }
  }
</style>
