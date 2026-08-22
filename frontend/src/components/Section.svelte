<script lang="ts">
  /* Wraps a dashboard section so it can be moved between zones and collapsed.
   *
   * Pointer events rather than HTML5 drag-and-drop: HTML5 DnD doesn't fire on
   * touch at all, and its drag image is not stylable. Pointer events cover
   * mouse, touch and pen with one code path.
   *
   * The handle is also a button, and arrow keys move the section — up and down
   * within its column, left and right between columns. Drag alone would make
   * rearranging mouse-only, which is a real exclusion rather than a nicety.
   *
   * AIM, THEN DROP. Dragging shows where the section WILL land — a line in the
   * target zone — and only moves it when the pointer is released. The earlier
   * version reordered live on every crossing, so the layout was rearranging
   * underneath the thing being aimed at, and each reorder had to re-anchor the
   * card to stop it jumping. That feedback loop was the whole source of the
   * oscillation, the stutter, and the FLIP bookkeeping that came with it. None
   * of it exists here, because nothing moves during the drag.
   *
   * COLLAPSING UNMOUNTS, rather than hiding with CSS. Two sections poll on a
   * timer — the activity timeline every 60s and history on its range's period
   * — so a merely-hidden section would go on fetching data nobody is looking
   * at. Unmounting stops that, at the cost of a refetch when it reopens, which
   * is the right trade for a panel you deliberately put away.
   */
  import { onDestroy } from 'svelte';
  import type { Snippet } from 'svelte';
  import type { Layout, Zone } from '../lib/layout.svelte';
  import { ZONE_LABEL } from '../lib/layout.svelte';

  interface Props {
    layout: Layout;
    id: string;
    children: Snippet;
  }
  const { layout, id, children }: Props = $props();

  let grabbed = $state(false);
  /** Lift, in pixels from where the pointer went down. */
  let offsetX = $state(0);
  let offsetY = $state(0);
  let anchorX = 0;
  let anchorY = 0;

  const label = $derived(layout.label(id));
  const zone = $derived(layout.zoneOf(id));
  const siblings = $derived(layout.inZone(zone));
  // Counted within the zone and against what is ON SCREEN. "3 of 5" across the
  // whole page describes a sequence the reader cannot see, now that the page is
  // three independent stacks rather than one.
  const position = $derived(`${siblings.indexOf(id) + 1} of ${siblings.length}, ${ZONE_LABEL[zone]}`);
  const collapsed = $derived(layout.isCollapsed(id));

  /** Squared distance from a point to a rect; 0 when inside. */
  function distance(r: DOMRect, px: number, py: number): number {
    const dx = Math.max(r.left - px, 0, px - r.right);
    const dy = Math.max(r.top - py, 0, py - r.bottom);
    return dx * dx + dy * dy;
  }

  /** The zone the pointer is over, falling back to the nearest one.
   *
   * The fallback is not a nicety. The zones do not tile the window — there are
   * gaps between the columns, margins either side, and everything below the
   * last card is outside all three. Without "nearest", the drop target would
   * blink out whenever the pointer strayed into any of that, which reads as the
   * drag having broken.
   */
  function zoneAt(px: number, py: number): HTMLElement | null {
    const els = [...document.querySelectorAll<HTMLElement>('[data-zone]')];
    if (!els.length) return null;

    let best = els[0];
    let bestD = Infinity;
    for (const el of els) {
      const d = distance(el.getBoundingClientRect(), px, py);
      if (d < bestD) {
        bestD = d;
        best = el;
      }
      if (d === 0) break;
    }
    return best;
  }

  /** How much of a full-width card's width, at each end, means "pair with me".
   *
   * The outer third rather than the half the gesture is described as. A half
   * leaves no room to aim BETWEEN two full-width cards: insert-above and
   * insert-below would only be reachable through the 16px gaps, and a 16px
   * target for an everyday action is not a target. At a third, both gestures
   * stay aimable and the edges — where you would naturally aim for "put it
   * beside this" — do the new thing. */
  const PAIR_EDGE = 0.3;

  /** The pair gesture: a full-width card aimed at the end of another one.
   *
   * Returns null for every case that is not this gesture, so the caller falls
   * through to ordinary line aiming. That includes the middle of a card, the
   * gaps between cards, a card already in a column, and the dragged card
   * itself — a card cannot pair with itself, and without that guard aiming at
   * your own edge would silently half-width the thing in your hand.
   */
  function pairAt(
    zoneEl: HTMLElement,
    px: number,
    py: number,
  ): { targetId: string; side: 'left' | 'right'; rect: { x: number; y: number; w: number; h: number } } | null {
    if (zoneEl.dataset.zone !== 'full') return null;
    if (layout.zoneOf(id) !== 'full') return null;

    const zr = zoneEl.getBoundingClientRect();
    for (const el of zoneEl.querySelectorAll<HTMLElement>(':scope > [data-slot]')) {
      const targetId = el.dataset.slot;
      if (!targetId || targetId === id) continue;

      const r = el.getBoundingClientRect();
      if (py < r.top || py > r.bottom) continue;

      const across = (px - r.left) / r.width;
      const side = across < PAIR_EDGE ? 'left' : across > 1 - PAIR_EDGE ? 'right' : null;
      if (!side) return null;

      return {
        targetId,
        side,
        rect: {
          x: r.left - zr.left + (side === 'left' ? 0 : r.width / 2),
          y: r.top - zr.top,
          w: r.width / 2,
          h: r.height,
        },
      };
    }
    return null;
  }

  /** Where in a zone the pointer is aiming, and where to draw the line.
   *
   * Midpoint comparison down a single stack, which is exact — unlike the
   * two-dimensional case this replaced, a column has only one axis to be on the
   * wrong side of.
   *
   * This card is excluded from the reckoning while remaining in the flow. It
   * keeps its space for the whole drag, so nothing reflows under the pointer;
   * and because the index counts only the OTHER cards, the gap it leaves behind
   * cannot shift the destination by one.
   */
  function aim(
    zoneEl: HTMLElement,
    py: number,
  ): { anchorId: string | null; before: boolean; y: number } {
    const cards = [...zoneEl.querySelectorAll<HTMLElement>(':scope > [data-slot]')].filter(
      (el) => el.dataset.slot !== id,
    );
    const zr = zoneEl.getBoundingClientRect();

    /* An empty column anchors on its BAND rather than on nothing. Returning a
       null anchor here would append to the end of the page-wide order, which
       is exactly the fall-to-the-bottom this structure was changed to fix. */
    if (!cards.length) {
      return {
        anchorId: zoneEl.dataset.bandLast || null,
        before: false,
        y: Math.min(24, zr.height / 2),
      };
    }

    for (const el of cards) {
      const r = el.getBoundingClientRect();
      if (py < r.top + r.height / 2) {
        return { anchorId: el.dataset.slot ?? null, before: true, y: r.top - zr.top - 8 };
      }
    }

    const last = cards[cards.length - 1];
    return {
      anchorId: last.dataset.slot ?? null,
      before: false,
      y: last.getBoundingClientRect().bottom - zr.top + 8,
    };
  }

  /* Move and release are tracked on the WINDOW, not via setPointerCapture on
   * the handle.
   *
   * Capture looks like the right tool and isn't: Safari drops a capture when
   * the capturing element is re-parented, after which no further pointermove or
   * pointerup arrives — the card freezes mid-drag and stays stuck, because the
   * pointerup that would have ended it never lands. Chrome happens to be more
   * forgiving, which is why this only showed up in Safari. The window is never
   * re-parented, so events keep flowing.
   */
  function startTracking() {
    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
    window.addEventListener('pointercancel', onCancel);
    window.addEventListener('keydown', onEscape);
  }

  function stopTracking() {
    window.removeEventListener('pointermove', onPointerMove);
    window.removeEventListener('pointerup', onPointerUp);
    window.removeEventListener('pointercancel', onCancel);
    window.removeEventListener('keydown', onEscape);
  }

  function onPointerDown(event: PointerEvent) {
    // Left button / primary contact only — a right-click on the handle should
    // open a context menu, not start a drag.
    if (event.button !== 0) return;
    event.preventDefault();

    grabbed = true;
    layout.dragId = id;
    anchorX = event.clientX;
    anchorY = event.clientY;
    offsetX = 0;
    offsetY = 0;
    startTracking();
  }

  function onPointerMove(event: PointerEvent) {
    if (!grabbed) return;

    /* Measured from where the pointer went down, not from the element's current
       box: a getBoundingClientRect() here reports the TRANSFORMED rect and
       would feed the lift back into its own input. */
    offsetX = event.clientX - anchorX;
    offsetY = event.clientY - anchorY;

    const zoneEl = zoneAt(event.clientX, event.clientY);
    if (!zoneEl) return;
    const z = zoneEl.dataset.zone as Zone;

    const band = Number(zoneEl.dataset.band ?? 0);

    const pair = pairAt(zoneEl, event.clientX, event.clientY);
    if (pair) {
      layout.drop = { kind: 'pair', zone: z, band, ...pair };
      return;
    }

    const { anchorId, before, y } = aim(zoneEl, event.clientY);
    layout.drop = { kind: 'line', zone: z, band, anchorId, before, y };
  }

  function finish() {
    grabbed = false;
    layout.dragId = null;
    layout.drop = null;
    offsetX = 0;
    offsetY = 0;
    stopTracking();
  }

  function onPointerUp() {
    if (!grabbed) return;
    const target = layout.drop;
    // Read before finish() clears it; applied after, so the card lands in a
    // page that is no longer in a drag state.
    finish();
    if (!target) return;
    if (target.kind === 'pair') layout.pairWith(id, target.targetId, target.side);
    else layout.placeAt(id, target.zone, target.anchorId, target.before);
  }

  /** Abandon without moving anything. Free to offer now that the drag is only
   *  an aim: there is nothing to undo. */
  function onCancel() {
    if (grabbed) finish();
  }

  function onEscape(event: KeyboardEvent) {
    if (event.key === 'Escape') onCancel();
  }

  // A drag interrupted by the component going away would otherwise leave
  // window listeners behind, and `grabbed` latched on in a detached closure.
  onDestroy(stopTracking);

  function onKeyDown(event: KeyboardEvent) {
    const moves: Record<string, () => void> = {
      ArrowUp: () => layout.moveInZone(id, -1),
      ArrowDown: () => layout.moveInZone(id, 1),
      ArrowLeft: () => layout.shiftZone(id, -1),
      ArrowRight: () => layout.shiftZone(id, 1),
    };
    const move = moves[event.key];
    if (!move) return;
    event.preventDefault();
    move();
  }
</script>

<div
  data-slot={id}
  class="slot"
  class:grabbed
  style:transform={grabbed && (offsetX || offsetY)
    ? `translate(${offsetX}px, ${offsetY}px)`
    : undefined}
>
  <button
    class="handle"
    aria-label={`Move ${label}. Currently ${position}. Arrow keys move it within and between columns.`}
    title={`Drag to move ${label}, or focus and use arrow keys`}
    onpointerdown={onPointerDown}
    onkeydown={onKeyDown}
  >
    <!-- Six dots: the conventional grip, and it reads as "grab me" without a
         label taking up space in an already dense header. -->
    <svg width="10" height="16" viewBox="0 0 10 16" aria-hidden="true">
      <circle cx="2.5" cy="3" r="1.2" />
      <circle cx="7.5" cy="3" r="1.2" />
      <circle cx="2.5" cy="8" r="1.2" />
      <circle cx="7.5" cy="8" r="1.2" />
      <circle cx="2.5" cy="13" r="1.2" />
      <circle cx="7.5" cy="13" r="1.2" />
    </svg>
  </button>

  <!-- The toggle stays in one place and rotates, rather than moving into the
       panel when collapsed. A control that changes position depending on the
       state it's in reads as two different controls. -->
  <button
    class="collapse"
    class:collapsed
    aria-expanded={!collapsed}
    aria-label={`${collapsed ? 'Expand' : 'Collapse'} ${label}`}
    title={`${collapsed ? 'Expand' : 'Collapse'} ${label}`}
    onclick={() => layout.toggleCollapsed(id)}
  >
    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
      <path d="M1 3.5 L5 7 L9 3.5" fill="none" stroke="currentColor" stroke-width="1.6" />
    </svg>
  </button>

  {#if collapsed}
    <!-- A stub that still names what's here: collapsing should tidy the page,
         not make you expand things to find out what they were.
         Also clickable, as a bigger target than a 10px chevron — the same
         reason a form label activates its input. The chevron above remains the
         control; this is the same action with more room.

         No "collapsed" caption. A single thin bar where a panel used to be
         already says so, and the state is carried properly anyway: the chevron
         has aria-expanded and this button is labelled "Expand {label}". -->
    <button
      class="panel stub"
      aria-label={`Expand ${label}`}
      onclick={() => layout.toggleCollapsed(id)}
    >
      <span class="eyebrow">{label}</span>
    </button>
  {:else}
    {@render children()}
  {/if}
</div>

<style>
  .slot {
    position: relative;
    /* A grid so the panel inside FILLS the slot. The slot already stretches to
       its row — grid items do by default — but the panel is a child of the
       slot rather than of the zone, so without this it kept its own height and
       the band's columns still ended on different lines.
       The handle is absolutely positioned, so the panel is the only thing this
       lays out. */
    display: grid;
    /* minmax(0, 1fr), NEVER an implicit track — the same rule `.sections`,
       `.zone` and `.cols` all state, and the one this grid was added without.
       An implicit track is `auto`, whose minimum is MIN-CONTENT, so the track
       grew to the widest table it contained instead of to the slot: measured
       970px inside an 860px half, which pushed the Models card past the page
       edge and made the whole page scroll sideways. Only Models showed it
       because its declared column widths sum highest — the bug was in every
       panel and visible in one. */
    grid-template-columns: minmax(0, 1fr);
  }

  .slot.grabbed {
    /* Not a pointer target while it is being carried — the cursor should
       address what is underneath, and the card is following the cursor
       anyway. */
    pointer-events: none;
    /* Lifted above its neighbours while moving, so it reads as picked up
       rather than as a gap opening beneath it. Still BELOW the drop line — a
       section is exactly as wide as the column it is aiming at, so an opaque
       card directly over its own destination would hide the one thing the drag
       exists to show. */
    z-index: 5;
    /* Slightly translucent and lifted. At 0.7 it blended into whatever dense
       table it was passing over and became hard to read; the shadow does the
       work of saying "picked up" that the transparency was being asked to do. */
    opacity: 0.85;
    box-shadow: 0 8px 24px rgb(0 0 0 / 0.35);
  }

  .handle {
    position: absolute;
    /* In the page's own left padding, so it never collides with the headers —
       which already carry a title at one end and controls at the other. The
       shell's 20px padding means this lands in the gutter at every width, so
       no narrow-screen special case is needed. */
    left: -20px;
    top: 14px;
    padding: 4px 3px;
    border-radius: var(--radius);
    color: var(--ink-muted);
    cursor: grab;
    /* Hidden until wanted: a permanent grip on every panel is visual noise on
       a page that's meant to read as an instrument panel. */
    opacity: 0;
    transition: opacity 120ms ease, color 120ms ease;
    touch-action: none;
  }

  .handle svg {
    display: block;
    fill: currentColor;
  }

  /* Shares the gutter with the drag handle, stacked beneath it. Both are
     section-level controls, so they belong together and outside the panel —
     the headers already carry a title at one end and their own controls at
     the other. */
  .collapse {
    position: absolute;
    left: -20px;
    top: 38px;
    padding: 4px 3px;
    border-radius: var(--radius);
    color: var(--ink-muted);
    cursor: pointer;
    opacity: 0;
    transition: opacity 120ms ease, color 120ms ease;
  }

  .collapse svg {
    display: block;
    transition: transform 140ms ease;
  }

  /* Points down when open (press to fold away), right when closed (press to
     open out) — the direction the content will move. */
  .collapse.collapsed svg {
    transform: rotate(-90deg);
  }

  /* A collapsed section is a single thin bar, so a control that only appears
     on hover of a 40px strip is easy to miss. Once folded, the chevron stays
     faintly visible as the marker for what is there. */
  .collapse.collapsed {
    opacity: 0.55;
  }

  .slot:hover .handle,
  .slot:hover .collapse,
  .handle:focus-visible,
  .collapse:focus-visible,
  .slot:hover .collapse.collapsed {
    opacity: 1;
  }

  .handle:hover,
  .collapse:hover {
    color: var(--ink);
  }

  /* Reads as a panel that's been folded away, not as a different kind of
     object: same frame and eyebrow as a real header, just nothing under it. */
  .stub {
    display: flex;
    align-items: baseline;
    width: 100%;
    padding: 14px 16px;
    text-align: left;
    cursor: pointer;
  }

  /* The frame lifts toward the foreground ink on hover — enough to read as
     interactive without inventing a colour the themes don't define. */
  .stub:hover {
    border-color: var(--ink-muted);
  }

  /* Matches h2.eyebrow, because this IS the card's title — just in its folded
     state. A collapsed section that renders its name more quietly than the
     open one would read as a lesser kind of thing rather than the same thing
     put away. (A span rather than an h2 here: the whole stub is a button, and
     a heading inside a button is a heading you cannot navigate to.) */
  .stub .eyebrow {
    color: var(--ink);
    font-weight: 700;
  }

  .handle:active {
    cursor: grabbing;
  }

  .slot.grabbed .handle {
    opacity: 1;
    color: var(--ink);
  }

  /* Touch has no hover, so a hover-revealed control is simply unreachable
     there. Show it permanently but faintly, and give it a bigger target than
     a fingertip needs to hunt for. */
  @media (pointer: coarse) {
    .handle {
      opacity: 0.45;
      padding: 8px 6px;
      left: -22px;
    }

    .collapse {
      opacity: 0.45;
      padding: 8px 6px;
      left: -22px;
      top: 46px;
    }
  }
</style>
