/** The reader's order for a card's panels (roadmap CP): System Activity's
 *  metrics, Temperatures' groups, Network Activity's groups.
 *
 * ONE ORDER PER CARD, used by every view of it. Dragged in the gauge view, it
 * also orders that card's charts and tables, so GPU temperature put first is
 * first however the card is drawn. Charts are not draggable themselves: a
 * drag on a chart already pans and zooms it.
 *
 * localStorage, per browser, like column order (CO); Reset layout clears it.
 */

import { applyOrder, moveBefore } from './column-order';

const KEY = 'spark-dash.card-order.v1';

function read(): Record<string, string[]> {
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) ?? 'null');
    if (!saved || typeof saved !== 'object' || Array.isArray(saved)) return {};
    const out: Record<string, string[]> = {};
    for (const [card, keys] of Object.entries(saved)) {
      if (Array.isArray(keys)) out[card] = keys.filter((k): k is string => typeof k === 'string');
    }
    return out;
  } catch {
    // The default order is the safe direction to fail in.
    return {};
  }
}

class ItemOrderStore {
  order = $state<Record<string, string[]>>(read());

  #save() {
    try {
      localStorage.setItem(KEY, JSON.stringify(this.order));
    } catch {
      // Still applied for this session.
    }
  }

  /** `items` in this card's order. Unknown stored keys are dropped and new
   *  items land after their default neighbour, as for columns. */
  ordered<T>(card: string, items: T[], keyOf: (item: T) => string): T[] {
    const wrapped = items.map((item) => ({ key: keyOf(item), item }));
    return applyOrder(wrapped, this.order[card] ?? []).map((w) => w.item);
  }

  /** Move `key` in front of `before` (null: to the end). `all` is every key
   *  the card has, in its current order, so hidden ones keep their place. */
  move(card: string, all: string[], key: string, before: string | null) {
    if (!all.includes(key) || (before !== null && !all.includes(before))) return;
    this.order = { ...this.order, [card]: moveBefore(all, key, before) };
    this.#save();
  }

  /** One place among the SHOWN keys, for the menus' arrows. */
  step(card: string, all: string[], shown: string[], key: string, dir: -1 | 1) {
    const i = shown.indexOf(key);
    if (i < 0) return;
    if (dir < 0 && i > 0) this.move(card, all, key, shown[i - 1]);
    else if (dir > 0 && i < shown.length - 1) this.move(card, all, key, shown[i + 2] ?? null);
  }

  reset() {
    this.order = {};
    try {
      localStorage.removeItem(KEY);
    } catch {
      // Still applied for this session.
    }
  }

  get customised(): boolean {
    return Object.values(this.order).some((keys) => keys.length > 0);
  }
}

export const itemOrder = new ItemOrderStore();

/** Which ways `key` can step among `shown`, for a menu row's arrows. */
export function steps(shown: string[], key: string): { canLeft: boolean; canRight: boolean } {
  const i = shown.indexOf(key);
  return { canLeft: i > 0, canRight: i >= 0 && i < shown.length - 1 };
}
