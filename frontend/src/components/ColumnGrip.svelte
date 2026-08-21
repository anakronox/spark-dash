<script lang="ts">
  /* Drag handle for resizing one column.
   *
   * THE HAZARD THIS IS SHAPED AROUND: the header is already a button.
   * `SortButton` fills the `<th>`, so a handle placed inside it would make
   * every resize also re-sort the table — and a 3px mis-aim would reorder the
   * data the reader was in the middle of measuring. So this is a sibling of
   * the button, sits above it, and stops its events reaching it.
   *
   * A pointerup that never moved is NOT a click on the sort control either:
   * stopping the event at pointerdown means the button never sees the gesture
   * start, so it cannot complete one.
   *
   * KEYBOARD RESIZING IS NOT OPTIONAL. This page has been careful about that
   * elsewhere — the compact card is focusable, the column menu and sort
   * controls are real buttons — and a resize only a mouse could reach would be
   * the one exception. `separator` is the ARIA role for exactly this.
   */
  import { MIN_COLUMN_PX } from '../lib/columns.svelte';

  interface Props {
    /** Current rendered width in px, measured from the header cell. */
    width: () => number;
    onresize: (px: number) => void;
    /** Back to the ColumnDef default — the escape from a column dragged too
     *  narrow to grab again, which is the one unrecoverable state here. */
    onreset: () => void;
    label: string;
  }
  const { width, onresize, onreset, label }: Props = $props();

  let dragging = $state(false);

  /** 16px is a visible step without being a jump; shift makes it coarse. */
  const STEP = 16;

  function onpointerdown(e: PointerEvent) {
    // The whole point: the sort button behind this must never see the gesture.
    e.preventDefault();
    e.stopPropagation();

    const startX = e.clientX;
    const startW = width();
    dragging = true;

    const move = (ev: PointerEvent) => onresize(startW + (ev.clientX - startX));
    const up = () => {
      dragging = false;
      globalThis.removeEventListener('pointermove', move);
      globalThis.removeEventListener('pointerup', up);
    };
    globalThis.addEventListener('pointermove', move);
    globalThis.addEventListener('pointerup', up);
  }

  function onkeydown(e: KeyboardEvent) {
    const step = e.shiftKey ? STEP * 4 : STEP;
    if (e.key === 'ArrowLeft') onresize(width() - step);
    else if (e.key === 'ArrowRight') onresize(width() + step);
    else if (e.key === 'Home' || e.key === 'Escape') onreset();
    else return;
    e.preventDefault();
    e.stopPropagation();
  }
</script>

<!-- A FOCUSABLE `separator` IS interactive per ARIA — the "window splitter"
     pattern, which is exactly this. The linter only knows the non-focusable
     variant, which is decorative, so both rules are suppressed here rather
     than downgrading the role to something less accurate. A `button` would be
     lint-clean and would lie: this performs no action, it holds a value the
     arrow keys change. -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<span
  class="grip absolute top-0 -right-1 z-[1] h-full w-2 cursor-col-resize bg-transparent
         focus-visible:outline-none"
  class:dragging
  role="separator"
  aria-orientation="vertical"
  aria-label="Resize {label} column"
  aria-valuenow={Math.round(width())}
  aria-valuemin={MIN_COLUMN_PX}
  tabindex="0"
  {onpointerdown}
  {onkeydown}
  ondblclick={(e) => {
    e.stopPropagation();
    onreset();
  }}
></span>

<style>
  /* SPIKE NOTE (roadmap AB). THE RESIDUAL IS THE INTERESTING PART.
     Layout moved to utilities above and reads fine. What could not move:

     - `::after` is the visible 2px cue inside an 8px grab area, and it needs
       three states (hover, focus-visible, dragging). Tailwind has `after:`
       variants, but each state carries its own inset, width and colour, so the
       result is a class string longer than the rule it replaces with the
       geometry scattered across variants instead of stated once.
     - `prefers-reduced-motion` is available as a variant but has to stack the
       motion-reduce and after- prefixes onto one pseudo-element utility.

     The comment you are reading is the actual finding. Originally the
     reasoning — why 8px of grab area for a 2px cue, why invisible until
     wanted — sat directly above the declarations it explained. Utilities have
     nowhere to put that, so it either floats up here away from the code it
     describes, or it is lost. */
  .grip::after {
    content: '';
    position: absolute;
    inset: 20% auto 20% 3px;
    width: 2px;
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

  @media (prefers-reduced-motion: reduce) {
    .grip::after {
      transition: none;
    }
  }
</style>
