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
  let offsetY = $state(0);
  /** Pointer position the current lift is measured from. Shifted on each
   *  reorder by however far the card's home moved, so the lift compensates
   *  instead of snapping. */
  let anchorY = 0;
  /** True while a swap is awaiting its DOM flush. */
  let settling = false;

  const label = $derived(layout.label(id));
  const position = $derived(`${index + 1} of ${layout.order.length}`);
  const collapsed = $derived(layout.isCollapsed(id));

  /** This card's top with the lift removed — where it actually sits. */
  function baseTop(): number {
    return host ? host.getBoundingClientRect().top - offsetY : 0;
  }

  /** The neighbour to trade places with, or null to stay put.
   *
   * ONE STEP AT A TIME, ONLY IN THE DIRECTION OF TRAVEL, and comparing this
   * card's own lifted edge against the neighbour's midpoint rather than asking
   * where the pointer is.
   *
   * All three matter, and together they are what stops the shudder when cards
   * overlap. A pointer-position test sitting near a boundary answers
   * differently on consecutive frames, so the order flips back and forth every
   * time the mouse breathes. Allowing multi-slot jumps makes that worse when
   * sections differ in height as much as a twelve-row table and a chart do.
   * And gating on direction means that once a swap has happened, reversing it
   * requires travelling back across the neighbour's midpoint — real hysteresis,
   * rather than a boundary the card can sit exactly on.
   */
  function neighbour(): number | null {
    if (!host?.parentElement) return null;
    const slots = [...host.parentElement.querySelectorAll('[data-slot]')];
    const self = host.getBoundingClientRect();

    if (offsetY > 0 && index < slots.length - 1) {
      const next = slots[index + 1].getBoundingClientRect();
      if (self.bottom > next.top + next.height / 2) return index + 1;
    }
    if (offsetY < 0 && index > 0) {
      const prev = slots[index - 1].getBoundingClientRect();
      if (self.top < prev.top + prev.height / 2) return index - 1;
    }
    return null;
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

    grabbed = true;
    layout.dragging = index;
    anchorY = event.clientY;
    offsetY = 0;
    startTracking();
  }

  async function onPointerMove(event: PointerEvent) {
    // `settling` holds off re-entry while the swap below awaits a DOM flush;
    // without it a burst of moves would each act on stale geometry.
    if (!grabbed || settling) return;

    /* Measured from where the pointer started, NOT from the element's current
     * box. getBoundingClientRect() reports the TRANSFORMED rect, so reading it
     * here fed the lift back into its own input: with the pointer held still,
     * the offset alternated between the real delta and zero on every frame,
     * which is the jitter this replaced. */
    offsetY = event.clientY - anchorY;

    const target = neighbour();
    if (target === null) return;

    settling = true;
    const before = baseTop();
    layout.move(index, target);
    // Wait for the reorder to land so the new home can be measured rather
    // than assumed — sections differ in height, and there is a gap between
    // them, so the shift is not a number worth guessing at.
    await tick();
    const after = baseTop();

    /* Compensate rather than reset. The card's home just moved by the
     * neighbour's height, so the lift shrinks by exactly that much and the
     * card stays visually still through the swap. Resetting the lift to zero
     * instead made it jump to its new slot while the pointer stood still,
     * which is the lurch you see as cards overlap. */
    anchorY += after - before;
    offsetY = event.clientY - anchorY;
    settling = false;
  }

  function onPointerUp() {
    if (!grabbed) return;
    grabbed = false;
    settling = false;
    layout.dragging = null;
    offsetY = 0;
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
      layout.move(index, index - 1);
    } else if (event.key === 'ArrowDown' && index < layout.order.length - 1) {
      event.preventDefault();
      layout.move(index, index + 1);
    }
  }
</script>

<div
  bind:this={host}
  data-slot={id}
  class="slot"
  class:grabbed
  style:transform={grabbed && offsetY ? `translateY(${offsetY}px)` : undefined}
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
         control; this is the same action with more room. -->
    <button
      class="panel stub"
      aria-label={`Expand ${label}`}
      onclick={() => layout.toggleCollapsed(id)}
    >
      <span class="eyebrow">{label}</span>
      <span class="dim">collapsed</span>
    </button>
  {:else}
    {@render children()}
  {/if}
</div>

<style>
  .slot {
    position: relative;
  }

  .slot.grabbed {
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
    justify-content: space-between;
    gap: 12px;
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
