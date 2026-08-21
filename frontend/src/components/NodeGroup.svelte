<script lang="ts">
  /* A draggable node or cluster card.
   *
   * SEPARATE FROM Section, deliberately. The two gestures look alike and are
   * not: a section has three zones, a band, a half-width state and a pairing
   * drop. A node card has one axis and no width to change — a cluster frame
   * spans the row because the frame means "these pool memory", and one
   * covering half a row would say something untrue about which nodes are
   * grouped. Generalising Section to cover both would have made the section
   * drag carry parameters that are always the same value here.
   *
   * WHAT MOVES IS THE GROUP. Dragging `danflashes` moves its members with it;
   * their order inside the frame stays cluster.yml's, which is also what
   * decides their colour.
   */
  import type { Layout } from '../lib/layout.svelte';

  interface Props {
    layout: Layout;
    /** Cluster name, or a standalone node's id. */
    groupKey: string;
    label: string;
    /** "2 of 3", for the handle's accessible name. */
    position: string;
    /** Every group currently on the page, in the order they render. */
    all: string[];
    /** True for a framed cluster, which spans the whole row in compact mode:
     *  the frame means "these pool memory", and one covering part of a row
     *  would say something untrue about which nodes are grouped. */
    framed: boolean;
    children: import('svelte').Snippet;
  }
  const { layout, groupKey, label, position, all, framed, children }: Props = $props();

  let grabbed = $state(false);
  let offsetY = $state(0);
  let anchorY = 0;

  /** Which card the pointer is aiming at, and which side of it.
   *
   * One axis in the full-width case, two once compact mode grids the cards
   * into columns — read from the grid's own track count rather than guessed,
   * because compact reflows at four breakpoints and a hardcoded assumption
   * would be wrong at three of them.
   */
  function aim(px: number, py: number) {
    const grid = document.querySelector<HTMLElement>('.node-grid');
    if (!grid) return null;
    const cards = [...grid.querySelectorAll<HTMLElement>('[data-group]')].filter(
      (el) => el.dataset.group !== groupKey,
    );
    if (!cards.length) return null;

    const tracks = getComputedStyle(grid).gridTemplateColumns.split(' ').filter(Boolean).length;
    const gr = grid.getBoundingClientRect();

    let best = cards[0];
    let bestD = Infinity;
    for (const el of cards) {
      const r = el.getBoundingClientRect();
      const dx = px - (r.left + r.width / 2);
      const dy = py - (r.top + r.height / 2);
      const d = dx * dx + dy * dy;
      if (d < bestD) {
        bestD = d;
        best = el;
      }
    }

    const r = best.getBoundingClientRect();
    // Single column: above or below. Gridded: left or right of centre within a
    // row, falling back to vertical when the pointer is on another row.
    const sameRow = tracks > 1 && py >= r.top && py <= r.bottom;
    const before = sameRow ? px < r.left + r.width / 2 : py < r.top + r.height / 2;

    return {
      anchorKey: best.dataset.group ?? null,
      before,
      y: before ? r.top - gr.top - 8 : r.bottom - gr.top + 8,
    };
  }

  function onPointerMove(event: PointerEvent) {
    if (!grabbed) return;
    offsetY = event.clientY - anchorY;
    layout.nodeDrop = aim(event.clientX, event.clientY);
  }

  function finish() {
    grabbed = false;
    layout.nodeDragKey = null;
    layout.nodeDrop = null;
    offsetY = 0;
    window.removeEventListener('pointermove', onPointerMove);
    window.removeEventListener('pointerup', onPointerUp);
    window.removeEventListener('pointercancel', finish);
  }

  function onPointerUp() {
    if (!grabbed) return;
    const target = layout.nodeDrop;
    // Read before finish() clears it, applied after — the card lands on a page
    // that is no longer mid-drag. Same order as Section, for the same reason.
    finish();
    if (target) layout.moveGroup(groupKey, target.anchorKey, target.before, all);
  }

  function onPointerDown(event: PointerEvent) {
    if (event.button !== 0) return;
    event.preventDefault();
    grabbed = true;
    layout.nodeDragKey = groupKey;
    anchorY = event.clientY;
    offsetY = 0;
    // On the window rather than via setPointerCapture — see Section: Safari
    // drops a capture when the capturing element is re-parented.
    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
    window.addEventListener('pointercancel', finish);
  }

  function onKeyDown(event: KeyboardEvent) {
    const delta = event.key === 'ArrowUp' ? -1 : event.key === 'ArrowDown' ? 1 : 0;
    if (!delta) return;
    event.preventDefault();
    const list = layout.orderGroups(all);
    const i = list.indexOf(groupKey);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= list.length) return;
    // Anchored on the card being swapped with, on the far side of it, so a
    // repeated press keeps travelling instead of oscillating around one spot.
    layout.moveGroup(groupKey, list[j], delta < 0, all);
  }
</script>

<div
  class="group"
  data-group={groupKey}
  data-group-framed={framed ? '' : undefined}
  class:grabbed
  style:transform={grabbed && offsetY ? `translateY(${offsetY}px)` : undefined}
>
  <button
    class="handle"
    aria-label={`Move ${label}. Currently ${position}. Arrow keys move it up and down.`}
    title={`Drag to move ${label}, or focus and use arrow keys`}
    onpointerdown={onPointerDown}
    onkeydown={onKeyDown}
  >
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
  .group {
    position: relative;
  }

  /* Lifted, not moved: the card keeps its place in the flow for the whole drag
     so nothing reflows under the pointer, exactly as a section does. */
  .grabbed {
    z-index: 5;
    opacity: 0.9;
  }

  .handle {
    position: absolute;
    /* In the shell's own left padding, so it never collides with the card's
       header — which already carries a name at one end and status at the
       other. */
    left: -20px;
    top: 14px;
    padding: 4px 3px;
    border-radius: var(--radius);
    color: var(--ink-muted);
    cursor: grab;
    opacity: 0;
    transition:
      opacity 120ms ease,
      color 120ms ease;
    touch-action: none;
  }

  .group:hover .handle,
  .handle:focus-visible {
    opacity: 1;
  }

  .handle:hover {
    color: var(--ink);
  }

  .grabbed .handle {
    cursor: grabbing;
    opacity: 1;
  }

  .handle svg {
    display: block;
    fill: currentColor;
  }
</style>
