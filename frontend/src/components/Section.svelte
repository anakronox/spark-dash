<script lang="ts">
  /* Wraps a dashboard section so it can be reordered and collapsed.
   *
   * Pointer events rather than HTML5 drag-and-drop: HTML5 DnD doesn't fire on
   * touch at all, and its drag image is not stylable. Pointer events cover
   * mouse, touch and pen with one code path.
   *
   * The handle is also a button, and arrow keys move the section. Drag alone
   * would make reordering mouse-only, which is a real exclusion rather than a
   * nicety — and it's the cheaper interaction anyway once you know it's there.
   *
   * COLLAPSING UNMOUNTS, rather than hiding with CSS. Two sections poll on a
   * timer — the activity timeline every 60s and history on its range's period
   * — so a merely-hidden section would go on fetching data nobody is looking
   * at. Unmounting stops that, at the cost of a refetch when it reopens, which
   * is the right trade for a panel you deliberately put away.
   */
  import { onDestroy, tick } from 'svelte';
  import type { Snippet } from 'svelte';
  import type { Layout } from '../lib/layout.svelte';

  interface Props {
    layout: Layout;
    index: number;
    id: string;
    children: Snippet;
  }
  const { layout, index, id, children }: Props = $props();

  let host = $state<HTMLElement | null>(null);
  let grabbed = $state(false);
  /** Lift, in pixels from where the pointer went down. */
  let offsetY = $state(0);
  let offsetX = $state(0);
  /** Pointer position the current lift is measured from. Shifted on each
   *  reorder by however far the card's home moved, so the lift compensates
   *  instead of snapping. */
  let anchorY = 0;
  let anchorX = 0;
  /** True while a reorder is awaiting its DOM flush.
   *
   * This gates ONLY the reorder decision, never the lift. The previous version
   * returned out of the whole move handler while settling, so the card stopped
   * following the pointer during every swap — which is exactly what made
   * dragging feel unresponsive. The card is now glued to the pointer at all
   * times and only the decision to reorder waits. */
  let reordering = false;

  const label = $derived(layout.label(id));
  // Counted against what is ON SCREEN. Announcing "3 of 5" when two are
  // hidden describes a page the listener cannot perceive.
  const position = $derived(`${index + 1} of ${layout.visible.length}`);
  const collapsed = $derived(layout.isCollapsed(id));

  /** This card's top with the lift removed — where it actually sits. */
  function baseTop(): number {
    return host ? host.getBoundingClientRect().top - offsetY : 0;
  }

  function baseLeft(): number {
    return host ? host.getBoundingClientRect().left - offsetX : 0;
  }

  /** The slot the POINTER is currently inside, or null.
   *
   * The pointer, not the card's centre. The handle sits in the page's left
   * padding, so the card's centre is half a card away from the cursor — asking
   * where the CENTRE is meant that pointing at a cell did not select it, and
   * the disagreement between the two made the target oscillate as the
   * compensation moved the card after each reorder.
   *
   * "Inside the cell" is the snap and the hysteresis at once: the cursor has to
   * travel fully into another cell to change the target, so there is no
   * trading places when two edges merely brush. Direction gating and
   * one-step-at-a-time are unnecessary as a result.
   *
   * Multi-cell jumps are allowed. They were the thrash risk before only
   * because neighbours teleported; with FLIP animating them, dragging across
   * three positions reads as three tiles gliding aside.
   */
  function targetSlot(px: number, py: number): number | null {
    if (!host?.parentElement) return null;
    const slots = [...host.parentElement.querySelectorAll('[data-slot]')];
    for (let i = 0; i < slots.length; i++) {
      if (i === index) continue;
      const r = slots[i].getBoundingClientRect();
      if (px >= r.left && px <= r.right && py >= r.top && py <= r.bottom) return i;
    }
    return null;
  }

  /** FLIP the siblings so a reorder reads as tiles making room.
   *
   * Without this they teleport into their new cells, which gives no sense of
   * the layout responding — the single biggest reason the first attempt felt
   * wrong. The dragged card is excluded: it already carries its own transform
   * following the pointer, and animating it too would fight that.
   */
  /** How long a sibling takes to glide into its new cell. Long enough to read
   *  as movement, short enough not to be in the way of the next reorder. */
  const FLIP_MS = 160;

  async function reorderWithFlip(target: number) {
    const container = host?.parentElement;
    if (!container) return;

    const cells = () =>
      [...container.querySelectorAll<HTMLElement>('[data-slot]')].map((el) => ({
        el,
        key: el.dataset.slot ?? '',
        rect: el.getBoundingClientRect(),
      }));

    const before = new Map(cells().map((c) => [c.key, c.rect]));

    const homeBefore = { x: baseLeft(), y: baseTop() };
    layout.moveVisible(index, target);
    await tick();
    const homeAfter = { x: baseLeft(), y: baseTop() };

    for (const { el, key, rect } of cells()) {
      if (key === id) continue; // the dragged card owns its own transform
      const prev = before.get(key);
      if (!prev) continue;
      const dx = prev.left - rect.left;
      const dy = prev.top - rect.top;
      if (!dx && !dy) continue;

      /* Invert, then play. The play step is triggered by a FORCED REFLOW, not
         requestAnimationFrame.
         rAF does not fire in a background tab, so an rAF-driven play left every
         sibling stuck at its inverted offset — invisible while the tab was
         hidden, and a visibly broken layout on returning to it. Reading
         offsetWidth flushes layout synchronously, which is all the browser
         needs to treat the next assignment as a transition rather than as the
         same frame. */
      el.style.transition = 'none';
      el.style.transform = `translate(${dx}px, ${dy}px)`;
      void el.offsetWidth;
      el.style.transition = `transform ${FLIP_MS}ms cubic-bezier(0.2, 0, 0, 1)`;
      el.style.transform = '';
      /* Clear the inline transition once it has played. Leaving it behind
         would mean the NEXT card picked up carries a 160ms transform
         transition, which smooths the lift and is felt as the drag lagging the
         pointer.

         On a TIMER, not transitionend. That event never fires when the tab is
         hidden — transitions do not run there — so an event-driven cleanup
         leaves the style behind exactly in the case nobody is watching, ready
         to bite on the next drag after the tab is focused again. */
      setTimeout(() => {
        el.style.transition = '';
      }, FLIP_MS + 20);
    }

    /* Compensate the lift rather than reset it. This card's home just moved,
       so the lift shrinks by exactly that much and the card stays visually
       still under the pointer through the reorder. Resetting instead made it
       jump to its new cell while the pointer stood still. */
    anchorX += homeAfter.x - homeBefore.x;
    anchorY += homeAfter.y - homeBefore.y;
  }

  /* Move and release are tracked on the WINDOW, not via setPointerCapture on
   * the handle.
   *
   * Capture looks like the right tool and isn't: reordering re-keys the
   * wrapper, so Svelte relocates the very node holding the capture. Safari
   * drops the capture when a capturing element is re-parented, after which no
   * further pointermove or pointerup arrives — the card freezes mid-drag and
   * stays stuck, because the pointerup that would have ended it never lands.
   * Chrome happens to be more forgiving, which is why this only showed up in
   * Safari. The window is never re-parented, so events keep flowing.
   */
  function startTracking() {
    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
    window.addEventListener('pointercancel', onPointerUp);
  }

  function stopTracking() {
    window.removeEventListener('pointermove', onPointerMove);
    window.removeEventListener('pointerup', onPointerUp);
    window.removeEventListener('pointercancel', onPointerUp);
  }

  function onPointerDown(event: PointerEvent) {
    // Left button / primary contact only — a right-click on the handle should
    // open a context menu, not start a drag.
    if (event.button !== 0) return;
    event.preventDefault();

    /* Defensive: a FLIP that was interrupted (a second drag started before
       its transition ended) could leave an inline transition on this card,
       which would smooth the lift. */
    if (host) {
      host.style.transition = '';
      host.style.transform = '';
    }

    grabbed = true;
    layout.dragging = index;
    anchorY = event.clientY;
    offsetY = 0;
    offsetX = 0;
    startTracking();
  }

  function onPointerMove(event: PointerEvent) {
    if (!grabbed) return;

    /* The lift updates on EVERY move, unconditionally. Measured from where the
       pointer went down, not from the element's current box: a
       getBoundingClientRect() here reports the TRANSFORMED rect and would feed
       the lift back into its own input, which is the jitter this replaced. */
    offsetX = event.clientX - anchorX;
    offsetY = event.clientY - anchorY;

    // Only the REORDER waits for its flush. Gating the whole handler on this
    // is what made the drag stutter.
    if (reordering) return;

    const target = targetSlot(event.clientX, event.clientY);
    if (target === null || target === index) return;

    reordering = true;
    void reorderWithFlip(target).finally(() => {
      reordering = false;
      // The pointer has kept moving while that settled; re-sync so the card
      // does not lag a frame behind by the width of the reorder.
      offsetX = event.clientX - anchorX;
      offsetY = event.clientY - anchorY;
    });
  }

  function onPointerUp() {
    if (!grabbed) return;
    grabbed = false;
    layout.dragging = null;
    offsetY = 0;
    offsetX = 0;
    reordering = false;
    stopTracking();
    // Persisted once, here, rather than on every swap — see Layout#save.
    layout.commit();
  }

  // A drag interrupted by the component going away would otherwise leave
  // window listeners behind, and `grabbed` latched on in a detached closure.
  onDestroy(stopTracking);

  function onKeyDown(event: KeyboardEvent) {
    if (event.key === 'ArrowUp' && index > 0) {
      event.preventDefault();
      layout.moveVisible(index, index - 1);
    } else if (event.key === 'ArrowDown' && index < layout.visible.length - 1) {
      event.preventDefault();
      layout.moveVisible(index, index + 1);
    }
  }
</script>

<div
  bind:this={host}
  data-slot={id}
  class="slot"
  class:full={layout.widthOf(id) === 'full'}
  class:grabbed
  style:transform={grabbed && (offsetX || offsetY)
    ? `translate(${offsetX}px, ${offsetY}px)`
    : undefined}
>
  <button
    class="handle"
    aria-label={`Move ${label}. Currently ${position}. Use arrow keys to reorder.`}
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
         has aria-expanded and this button is labelled "Expand {label}". A word
         that restates what the layout already shows is just something else to
         read on every collapsed row. -->
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
    /* Default half — one column of the two. A section that wants the row says
       so with .full. Below the grid's breakpoint there is only one column, so
       this is a no-op there and everything is full width anyway. */
    grid-column: span 1;
  }

  .slot.full {
    grid-column: 1 / -1;
  }

  .slot.grabbed {
    /* Not a pointer target while it is being carried — the cursor should
       address what is underneath, and the card is following the cursor
       anyway. */
    pointer-events: none;
    /* Lifted above its neighbours while moving, so it reads as picked up
       rather than as a gap opening beneath it. */
    z-index: 5;
    opacity: 0.9;
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
     interactive without inventing a colour the themes don't define. Every
     theme sets --rule and --ink-muted, so this works across all of them. */
  .stub:hover {
    border-color: var(--ink-muted);
  }

  .stub .eyebrow {
    color: var(--ink);
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
