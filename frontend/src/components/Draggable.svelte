<script lang="ts">
  /* Wraps a dashboard section so it can be reordered.
   *
   * Pointer events rather than HTML5 drag-and-drop: HTML5 DnD doesn't fire on
   * touch at all, and its drag image is not stylable. Pointer events cover
   * mouse, touch and pen with one code path.
   *
   * The handle is also a button, and arrow keys move the section. Drag alone
   * would make reordering mouse-only, which is a real exclusion rather than a
   * nicety — and it's the cheaper interaction anyway once you know it's there.
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

  {@render children()}
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

  .slot:hover .handle,
  .handle:focus-visible {
    opacity: 1;
  }

  .handle:hover {
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
  }
</style>
