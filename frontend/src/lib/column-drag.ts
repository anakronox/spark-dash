/** Drag a column header to move the column (roadmap CO).
 *
 * `use:reorderable={{ view: cols, key: c.key }}` on each header cell.
 *
 * THE HEADER IS ALREADY A SORT BUTTON. A drag only starts once the pointer has
 * moved DRAG_PX, so a click still sorts, and the click that ends a drag is
 * swallowed so a move never re-sorts the table too. The resize grip stops its
 * own pointerdown, so grabbing a column edge never starts a move.
 *
 * The drop marker is an attribute on the header it lands beside, drawn in
 * app.css, so no element is added to the table while it is being measured.
 */

import type { ColumnView } from './columns.svelte';
import { dropBefore, isNoOp } from './column-order';
import type { HeaderEdge } from './column-order';

/** Far enough that a click with a little wobble is still a click. */
const DRAG_PX = 5;

interface Params {
  view: ColumnView;
  key: string;
}

export function reorderable(th: HTMLElement, params: Params) {
  let { view, key } = params;

  const label = () => {
    th.dataset.colKey = key;
    if (view.columns.find((c) => c.key === key)?.required) th.dataset.pinned = '';
    else delete th.dataset.pinned;
  };
  label();

  /** The movable headers in this row, left to right, as drawn now. */
  const edges = (): { els: HTMLElement[]; edges: HeaderEdge[] } => {
    const els = [
      ...(th.parentElement?.querySelectorAll<HTMLElement>(':scope > th[data-col-key]:not([data-pinned])') ?? []),
    ];
    return {
      els,
      edges: els.map((el) => {
        const r = el.getBoundingClientRect();
        return { key: el.dataset.colKey ?? '', left: r.left, right: r.right };
      }),
    };
  };

  const clearMarks = (els: HTMLElement[]) => {
    for (const el of els) delete el.dataset.drop;
  };

  function onpointerdown(e: PointerEvent) {
    if (e.button !== 0 || 'pinned' in th.dataset) return;
    const startX = e.clientX;
    let dragging = false;
    let target: string | null | undefined;
    let row = edges();

    const move = (ev: PointerEvent) => {
      if (!dragging) {
        if (Math.abs(ev.clientX - startX) < DRAG_PX) return;
        dragging = true;
        row = edges();
        th.dataset.dragging = '';
        document.documentElement.dataset.reordering = '';
      }
      ev.preventDefault();
      const before = dropBefore(row.edges, ev.clientX);
      clearMarks(row.els);
      const shown = row.edges.map((x) => x.key);
      if (isNoOp(shown, key, before)) {
        target = undefined;
        return;
      }
      target = before;
      const mark = before === null ? row.els[row.els.length - 1] : row.els[shown.indexOf(before)];
      if (mark) mark.dataset.drop = before === null ? 'after' : 'before';
    };

    const up = () => {
      globalThis.removeEventListener('pointermove', move);
      globalThis.removeEventListener('pointerup', up);
      globalThis.removeEventListener('pointercancel', up);
      if (!dragging) return;
      clearMarks(row.els);
      delete th.dataset.dragging;
      delete document.documentElement.dataset.reordering;
      // The click that follows this pointerup must not sort. Removed on the
      // next task in case it never comes (released outside the header).
      const swallow = (ce: Event) => {
        ce.stopPropagation();
        ce.preventDefault();
      };
      th.addEventListener('click', swallow, { capture: true, once: true });
      setTimeout(() => th.removeEventListener('click', swallow, { capture: true }), 0);
      if (target !== undefined) view.move(key, target);
    };

    globalThis.addEventListener('pointermove', move);
    globalThis.addEventListener('pointerup', up);
    globalThis.addEventListener('pointercancel', up);
  }

  th.addEventListener('pointerdown', onpointerdown);
  return {
    update(next: Params) {
      ({ view, key } = next);
      label();
    },
    destroy() {
      th.removeEventListener('pointerdown', onpointerdown);
    },
  };
}
