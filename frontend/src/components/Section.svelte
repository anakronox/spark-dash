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

  const label = $derived(layout.label(id));
  const position = $derived(`${index + 1} of ${layout.order.length}`);
  const collapsed = $derived(layout.isCollapsed(id));

  /** Which index the pointer is currently over.
   *
   * Measured from sibling geometry rather than tracked with dragover handlers
   * on every target: fewer moving parts, and it stays correct when sections
   * have wildly different heights (a table of twelve models next to a chart).
   */
  function indexAt(clientY: number): number {
    if (!host?.parentElement) return index;
    const slots = [...host.parentElement.querySelectorAll('[data-slot]')];
    for (let i = 0; i < slots.length; i++) {
      const box = slots[i].getBoundingClientRect();
      if (clientY < box.top + box.height / 2) return i;
    }
    return slots.length - 1;
  }

  function onPointerDown(event: PointerEvent) {
    // Left button / primary contact only — a right-click on the handle should
    // open a context menu, not start a drag.
    if (event.button !== 0) return;
    event.preventDefault();

    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    grabbed = true;
    layout.dragging = index;
    offsetY = 0;
  }

  function onPointerMove(event: PointerEvent) {
    if (!grabbed || !host) return;
    const box = host.getBoundingClientRect();
    offsetY = event.clientY - (box.top + box.height / 2);

    const target = indexAt(event.clientY);
    if (target !== index) {
      layout.move(index, target);
      // The wrapper is re-keyed at its new index, so the visual offset from
      // the old position is no longer meaningful.
      offsetY = 0;
    }
  }

  function onPointerUp(event: PointerEvent) {
    if (!grabbed) return;
    (event.currentTarget as HTMLElement).releasePointerCapture(event.pointerId);
    grabbed = false;
    layout.dragging = null;
    offsetY = 0;
  }

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
    onpointermove={onPointerMove}
    onpointerup={onPointerUp}
    onpointercancel={onPointerUp}
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
