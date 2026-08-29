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
  import {
    ZONE_LABEL,
    MIN_ROWS,
    MAX_ROWS,
  } from '../lib/layout.svelte';
  import CardGrip from './CardGrip.svelte';

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

  /* --- RESIZE -------------------------------------------------------------
   *
   * WHAT A CARD'S HEIGHT IS MADE OF depends on what the card is drawing, and
   * the two units are not interchangeable:
   *
   *   - a CHART card's height is its plot height, continuous, in pixels;
   *   - a TABLE card's height is its row cap, discrete, in rows.
   *
   * So the grip reports pixels and this decides what they mean, by looking at
   * what is rendered rather than by consulting a list. That is not a shortcut
   * around a static rule — it is the only thing that answers correctly for
   * Network Activity, which draws a table or a grid of charts depending on a
   * mode of its own that this component cannot see.
   *
   * A ROW CAP APPLIES PER TABLE, and a plot height applies per ROW of charts.
   * Both mean the card grows by a multiple of what the reader dragged: six
   * tables on Temperatures, three rows of charts on System Activity. Dividing
   * the pointer delta by that multiple is what makes the corner follow the
   * pointer instead of leaping away from it — the earlier version did not, and
   * had to warn "applies to all 11 charts" in its label to explain why.
   */

  let slotEl: HTMLElement | undefined = $state();
  let mode = $state<'plot' | 'rows'>('rows');
  /** Pixels the CARD grows per one unit of the value being dragged. */
  let pxPerUnit = 1;
  let startValue = 0;

  /** The vertical module, read from CSS so there is one source for it.
   *
   * Only a FALLBACK for the frame before a table has laid out — the measured
   * row is preferred, and has to be. A rendered row is not reliably exactly one
   * module: at dpr 2 with a table starting on a fractional y, individual rows
   * snap to ±0.5px, and the last row of a table has no bottom border at all.
   * Measured on the live page: `models` came to exactly 25.000px per row,
   * `processes` to 25.167 and `network` to 24.938.
   *
   * That is fine, and it is why card heights are set by the GRID rather than by
   * accumulating row heights. Grid tracks are computed in layout units and do
   * not accumulate snapping error, so two columns stay aligned even when a row
   * inside one of them renders half a pixel tall. What must never be done is
   * dividing a pixel budget by a nominal 25 — hence `measure()`. */
  /** Resolved once; the module does not vary by theme. */
  let unitCache = 0;

  function rowUnit(): number {
    if (unitCache) return unitCache;
    /* MEASURED FROM A PROBE, not read from the property.
     * `getComputedStyle().getPropertyValue('--row-unit')` returns the literal
     * `calc(14px + 2 * 5px + 1px)` — custom properties are substituted, not
     * computed — so parseFloat gives NaN and any `|| 25` fallback beside it
     * hides the fact that the token was never read at all. Restating the
     * arithmetic in JS would be the same formula in two places. An element
     * asked for that height resolves it the way CSS does, once. */
    const probe = document.createElement('div');
    probe.style.cssText = 'position:absolute;visibility:hidden;height:var(--row-unit)';
    document.body.appendChild(probe);
    unitCache = probe.getBoundingClientRect().height;
    probe.remove();
    return unitCache;
  }

  /** Re-read what this card is drawing.
   *
   * Driven by a MutationObserver, because the answer changes at moments no
   * pointer is involved: charts arrive from a fetch after mount, and Network
   * Activity swaps its whole body between a table and a chart grid from its own
   * controls.
   *
   * A ResizeObserver was the obvious choice and is the WRONG SIGNAL, measured:
   * System Activity's plot replaces a 140px "Loading…" placeholder with a 132px
   * plot plus its caption, so the card's box lands within a pixel or two of
   * where it started and the observer never fires a second time. The card was
   * still reporting itself as a table of rows minutes after it had drawn a
   * chart. What actually changes is the DOM, so that is what to watch.
   *
   * Coalesced to one read per frame: a paging table mutates its rows on every
   * poll, and this would otherwise re-query the subtree once per row.
   *
   * It cannot loop: `mode` and `pxPerUnit` feed the grip's aria values and its
   * arithmetic, never the layout, so nothing this writes can change the DOM
   * that triggered it. */
  function measure() {
    if (!slotEl) return;

    const unit = rowUnit();
    cardRows = Math.max(1, Math.ceil((slotEl.getBoundingClientRect().height + GAP_PX) / unit));

    const plots = slotEl.querySelectorAll<HTMLElement>('.uplot');
    if (plots.length) {
      mode = 'plot';
      // Rows of the chart GRID, counted by distinct vertical offset. The grid
      // is 1-4 across depending on width, so the count cannot be derived from
      // the number of charts.
      pxPerUnit = new Set([...plots].map((p) => p.offsetTop)).size || 1;
      return;
    }

    mode = 'rows';
    const bodies = slotEl.querySelectorAll('tbody');
    const row = slotEl.querySelector<HTMLElement>('tbody tr');
    pxPerUnit = (row?.offsetHeight || rowUnit()) * Math.max(1, bodies.length);
  }

  /** How many modules this card spans in packed mode.
   *
   * THE LOOP THIS IS SHAPED AROUND: a span that made the card taller would be
   * measured on the next pass as the new natural height, spanning further every
   * frame. `align-self: start` is what breaks it — the card keeps its own
   * height inside whatever span it is given, so what is measured never depends
   * on what was written. `effect_update_depth_exceeded` is compiled out of
   * production builds, so a loop here would throw in dev and silently spin for
   * a reader.
   *
   * The gap is inside the span rather than between the tracks: `row-gap: 16px`
   * on 25px tracks would put every card after the first at 25n + 16, which is
   * never on the grid, and the whole point is that it is. */
  const GAP_PX = 16;
  let cardRows = $state(1);

  function onResizeStart() {
    measure();
    if (mode === 'plot') {
      startValue = layout.plotHeight(id);
      return;
    }
    startValue = layout.rowChoice(id);
    /* `0` is the uncapped sentinel, not a count, so a drag from it would start
       the arithmetic at zero rows and snap the card shut on the first pixel.
       Start from what is actually on screen instead — which is what the reader
       is looking at when they grab the corner. */
    if (startValue === 0) {
      const shown = slotEl?.querySelectorAll('tbody tr').length ?? 0;
      const bodies = Math.max(1, slotEl?.querySelectorAll('tbody').length ?? 1);
      startValue = Math.max(MIN_ROWS, Math.round(shown / bodies));
    }
  }

  /** A row count arrived at by gesture, which must never land on `0`.
   *
   * `0` is the UNCAPPED sentinel, not a count, so arithmetic that passes
   * through it turns "as small as this card goes" into "show me everything" —
   * measured: forty ArrowUps from 12 rows ended on `all rows`, with the card
   * twice the size it started. The sentinel stays reachable from the settings
   * list, where picking "all" is a deliberate act; it is not reachable by
   * dragging past the bottom. */
  function dragRows(n: number): number {
    return Math.max(MIN_ROWS, Math.min(MAX_ROWS, Math.round(n)));
  }

  function onResizeMove(dy: number) {
    /* SNAPPED TO THE MODULE, in the card's own units.
     *
     * `pxPerUnit` already scales the pointer to what the card grows by, so the
     * corner tracks the cursor in both modes. What it did not do was land on
     * anything in particular: a table stepped a whole row because the row count
     * is an integer, while a chart card slid continuously and could stop at any
     * pixel. Quantising the pointer travel first means BOTH move a whole table
     * row at a time, which is the whole reason the columns line up. */
    const unit = rowUnit();
    const delta = (Math.round(dy / unit) * unit) / pxPerUnit;
    if (mode === 'plot') layout.setPlotHeight(id, startValue + delta);
    else layout.setRows(id, dragRows(startValue + delta));
  }

  /** Keyboard step: one table row of CARD height, in either mode. Shift makes
   *  it coarse, the same as every other grip on the page.
   *
   *  A chart card's step is therefore one module divided by the number of chart
   *  ROWS — eight charts two-across grow the card by four times what the plot
   *  gained — so the card moves exactly one row per press whatever the grid
   *  happens to be. It used to be a flat 16px of plot, which moved the card by
   *  a different amount on every card and none of them a row. */
  function onResizeStep(dir: -1 | 1, coarse: boolean) {
    measure();
    if (mode === 'plot') {
      const step = (rowUnit() / pxPerUnit) * (coarse ? 4 : 1);
      layout.setPlotHeight(id, layout.plotHeight(id) + dir * step);
      return;
    }
    /* An uncapped card steps from what it is SHOWING, not from the sentinel:
       arrowing down from "all" should add a row to what is on screen, and
       arrowing up should start trimming it. */
    const now = layout.rowChoice(id) || (slotEl?.querySelectorAll('tbody tr').length ?? MIN_ROWS);
    layout.setRows(id, dragRows(now + dir * (coarse ? 5 : 1)));
  }

  function onResizeReset() {
    measure();
    if (mode === 'plot') layout.resetPlotHeight(id);
    else layout.resetRows(id);
  }

  $effect(() => {
    const el = slotEl;
    if (!el) return;

    measure();

    /* Coalesced with a TIMER, not requestAnimationFrame, and the difference is
       a real bug rather than a preference.
       rAF does not run in a hidden or occluded tab. With a `queued` flag
       guarding it, one notification arriving while the tab is in the
       background sets the flag, the frame never comes, the flag is never
       cleared — and every future mutation returns early for the life of the
       component. Measured: the card's span froze at its pre-content value of 4
       while the card itself grew to 668px, so it overlapped its neighbour, and
       no amount of poking the DOM would shake it loose. This dashboard's whole
       job is to sit on a second monitor, which is exactly where that happens.
       A timer is throttled in the background but it does fire, and
       `getBoundingClientRect()` flushes layout itself, so rAF bought nothing
       here anyway. */
    let queued: ReturnType<typeof setTimeout> | 0 = 0;
    const soon = () => {
      if (queued) return;
      queued = setTimeout(() => {
        queued = 0;
        measure();
      }, 0);
    };

    /* BOTH observers, because they answer different questions and each misses
       what the other catches. The MutationObserver is the one that notices a
       chart arriving where a placeholder was — measured, a ResizeObserver does
       NOT fire for that, because the 132px plot lands within a pixel of the
       140px placeholder it replaced. The ResizeObserver is the one that
       notices a card growing without its structure changing, which is what the
       span depends on. */
    const mo = new MutationObserver(soon);
    mo.observe(el, { childList: true, subtree: true });
    const ro = new ResizeObserver(soon);
    ro.observe(el);

    return () => {
      mo.disconnect();
      ro.disconnect();
      if (queued) clearTimeout(queued);
    };
  });

  /* A chart card reports the height it OCCUPIES, a table card the rows it
     SHOWS. Both are counted in table rows, which is the unit the corner moves
     in; they differ because those are the two different questions a reader has
     about the two kinds of card. Pixels used to be reported here, and named a
     quantity nothing else on the page is measured in. */
  const resizeValue = $derived(mode === 'plot' ? cardRows : layout.rowChoice(id));
  const resizeText = $derived(
    mode === 'plot'
      ? `${cardRows} row${cardRows === 1 ? '' : 's'} tall`
      : resizeValue === 0
        ? 'all rows'
        : `${resizeValue} row${resizeValue === 1 ? '' : 's'}`,
  );

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
  bind:this={slotEl}
  data-slot={id}
  class="slot"
  class:quantised={layout.bandMode === 'packed'}
  style:--card-rows={cardRows}
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
    class="fold"
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

    <!-- Not on a collapsed card: there is nothing to resize but a 40px stub,
         and a resize corner on it would offer a gesture that cannot do
         anything. -->
    <CardGrip
      onstart={onResizeStart}
      onmove={onResizeMove}
      onstep={onResizeStep}
      onreset={onResizeReset}
      label={label}
      valuenow={Math.round(resizeValue)}
      valuemin={MIN_ROWS}
      valuemax={MAX_ROWS}
      valuetext={resizeText}
    />
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

  /* Packed mode only. Aligned mode's zone is a subgrid of the band's rows and
     has its own answer to where a card sits; a span would fight it. */
  .slot.quantised {
    grid-row: span var(--card-rows, 1);
    /* The line that keeps the measurement honest — see cardRows. */
    align-self: start;
    margin-bottom: 16px;
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
  /* NOT `.collapse`, and the name is the whole point.
   *
   * `collapse` is a Tailwind utility — `visibility: collapse` — and Tailwind v4
   * generates a utility for any candidate string it finds in the markup,
   * including a class name of our own that happens to match. Utilities sit in
   * `@layer utilities` while these scoped rules are unlayered, so ours win
   * every property they SET. `visibility` was not one of them, so the control
   * was `visibility: collapse` on every section on the page: present in the
   * DOM, correctly positioned, measurable — and impossible to see or click.
   *
   * Pinned by test_no_scoped_class_shadows_a_tailwind_utility, which builds the
   * CSS and compares the two sets. The failure is silent by construction: no
   * error, no warning, and nothing wrong with either file on its own. */
  .fold {
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

  .fold svg {
    display: block;
    transition: transform 140ms ease;
  }

  /* Points down when open (press to fold away), right when closed (press to
     open out) — the direction the content will move. */
  .fold.collapsed svg {
    transform: rotate(-90deg);
  }

  /* A collapsed section is a single thin bar, so a control that only appears
     on hover of a 40px strip is easy to miss. Once folded, the chevron stays
     faintly visible as the marker for what is there. */
  .fold.collapsed {
    opacity: 0.55;
  }

  .slot:hover .handle,
  .slot:hover .fold,
  .handle:focus-visible,
  .fold:focus-visible,
  .slot:hover .fold.collapsed {
    opacity: 1;
  }

  .handle:hover,
  .fold:hover {
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

    .fold {
      opacity: 0.45;
      padding: 8px 6px;
      left: -22px;
      top: 46px;
    }
  }
</style>
