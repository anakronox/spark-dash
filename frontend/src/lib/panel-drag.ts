/** Drag a gauge panel to move it (roadmap CP).
 *
 * `use:reorderablePanel={{ card, key, all }}` on each panel, or undefined for
 * a panel that does not move. The column drag (column-drag.ts) on a wrapping
 * grid: the drop is found in two dimensions, and a press on a control inside
 * the panel (a button, a menu) is that control's, never a drag.
 */

import { dropBeforeGrid, isNoOp } from './column-order';
import type { ItemRect } from './column-order';
import { itemOrder } from './item-order.svelte';

const DRAG_PX = 5;

export interface PanelDrag {
  /** The card's order key: `trends.metrics`, `thermal.groups`, ... */
  card: string;
  key: string;
  /** Every key the card has, in its current order, hidden ones included. */
  all: () => string[];
}

export function reorderablePanel(el: HTMLElement, params: PanelDrag | undefined) {
  let p = params;

  const label = () => {
    if (p) el.dataset.itemKey = p.key;
    else delete el.dataset.itemKey;
  };
  label();

  const siblings = (): { els: HTMLElement[]; rects: ItemRect[] } => {
    const els = [...(el.parentElement?.querySelectorAll<HTMLElement>(':scope > [data-item-key]') ?? [])];
    return {
      els,
      rects: els.map((e) => {
        const r = e.getBoundingClientRect();
        return { key: e.dataset.itemKey ?? '', left: r.left, right: r.right, top: r.top, bottom: r.bottom };
      }),
    };
  };

  function onpointerdown(e: PointerEvent) {
    if (!p || e.button !== 0) return;
    if ((e.target as Element).closest('button, input, select, a, [role="separator"], [role="menu"], [role="group"]')) return;
    const drag = p;
    const startX = e.clientX;
    const startY = e.clientY;
    let dragging = false;
    let target: string | null | undefined;
    let row = siblings();

    const clear = () => {
      for (const s of row.els) delete s.dataset.drop;
    };

    const move = (ev: PointerEvent) => {
      if (!dragging) {
        if (Math.hypot(ev.clientX - startX, ev.clientY - startY) < DRAG_PX) return;
        dragging = true;
        row = siblings();
        // The whole item: Network's out panel goes with its in panel.
        for (const s of row.els) if (s.dataset.itemKey === drag.key) s.dataset.dragging = '';
        document.documentElement.dataset.reordering = '';
      }
      ev.preventDefault();
      const before = dropBeforeGrid(row.rects, ev.clientX, ev.clientY);
      clear();
      // One entry per item: Network's in and out panels share a key.
      const shown = [...new Set(row.rects.map((r) => r.key))];
      if (isNoOp(shown, drag.key, before)) {
        target = undefined;
        return;
      }
      target = before;
      const mark =
        before === null
          ? row.els[row.els.length - 1]
          : row.els.find((s) => s.dataset.itemKey === before);
      if (mark) mark.dataset.drop = before === null ? 'after' : 'before';
    };

    const up = () => {
      globalThis.removeEventListener('pointermove', move);
      globalThis.removeEventListener('pointerup', up);
      globalThis.removeEventListener('pointercancel', up);
      if (!dragging) return;
      clear();
      for (const s of row.els) delete s.dataset.dragging;
      delete document.documentElement.dataset.reordering;
      const swallow = (ce: Event) => {
        ce.stopPropagation();
        ce.preventDefault();
      };
      el.addEventListener('click', swallow, { capture: true, once: true });
      setTimeout(() => el.removeEventListener('click', swallow, { capture: true }), 0);
      if (target !== undefined) itemOrder.move(drag.card, drag.all(), drag.key, target);
    };

    globalThis.addEventListener('pointermove', move);
    globalThis.addEventListener('pointerup', up);
    globalThis.addEventListener('pointercancel', up);
  }

  el.addEventListener('pointerdown', onpointerdown);
  return {
    update(next: PanelDrag | undefined) {
      p = next;
      label();
    },
    destroy() {
      el.removeEventListener('pointerdown', onpointerdown);
    },
  };
}
