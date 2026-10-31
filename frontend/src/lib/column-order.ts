/** Column order: the arithmetic behind dragging a header (roadmap CO).
 *
 * Plain TypeScript with no runes and no DOM, so tests/js runs it for real.
 *
 * REQUIRED COLUMNS STAY FIRST. They are the row's identity -- `model`,
 * `process`, `interface` -- and a row of numbers is unreadable until its label
 * has been read. Only the rest move, and the stored order names only them.
 */

export interface Orderable {
  key: string;
  required?: boolean;
}

/** The columns in the reader's order.
 *
 * A stored key the table no longer has is dropped. A column the stored order
 * does not name -- one added since the reader dragged -- goes in after the
 * column it follows by default, so a new column lands where it was designed
 * to rather than at the end. */
export function applyOrder<T extends Orderable>(columns: T[], stored: string[]): T[] {
  const pinned = columns.filter((c) => c.required);
  const movable = columns.filter((c) => !c.required);
  if (!stored.length) return [...pinned, ...movable];

  const byKey = new Map(movable.map((c) => [c.key, c]));
  const out: T[] = [];
  for (const key of stored) {
    const col = byKey.get(key);
    if (col && !out.includes(col)) out.push(col);
  }
  movable.forEach((col, i) => {
    if (out.includes(col)) return;
    let at = 0;
    for (let j = i - 1; j >= 0; j--) {
      const before = out.indexOf(movable[j]);
      if (before >= 0) {
        at = before + 1;
        break;
      }
    }
    out.splice(at, 0, col);
  });
  return [...pinned, ...out];
}

/** `order` with `key` moved in front of `before`, or to the end for null. */
export function moveBefore(order: string[], key: string, before: string | null): string[] {
  if (key === before) return order;
  const rest = order.filter((k) => k !== key);
  const at = before === null ? rest.length : rest.indexOf(before);
  if (at < 0) return order;
  return [...rest.slice(0, at), key, ...rest.slice(at)];
}

export interface HeaderEdge {
  key: string;
  left: number;
  right: number;
}

/** Which header a drop at `x` lands in front of: the first whose middle is
 *  right of the pointer, or null for after the last. `edges` are the movable
 *  headers, left to right. */
export function dropBefore(edges: HeaderEdge[], x: number): string | null {
  for (const e of edges) if (x < (e.left + e.right) / 2) return e.key;
  return null;
}

/** True when dropping `key` in front of `before` would leave it where it is:
 *  in front of itself, or in front of the column already after it. */
export function isNoOp(visible: string[], key: string, before: string | null): boolean {
  const i = visible.indexOf(key);
  return before === key || (before === null ? i === visible.length - 1 : visible[i + 1] === before);
}

export interface ItemRect extends HeaderEdge {
  top: number;
  bottom: number;
}

/** A drop at (x, y) on a wrapping grid of panels (gauges, CP): the panel
 *  under the pointer, or the nearest one when it is in a gap, then its left or
 *  right half. Right half means in front of the next item, or null at the end.
 *  `items` are in reading order; panels sharing a key (Network's in and out
 *  of one group) are one item, so "next" skips the partner. */
export function dropBeforeGrid(items: ItemRect[], x: number, y: number): string | null {
  if (!items.length) return null;
  const dist = (r: ItemRect) =>
    Math.hypot(Math.max(r.left - x, 0, x - r.right), Math.max(r.top - y, 0, y - r.bottom));
  // The pointer's own row first: empty space after a row's last panel means
  // the end of that row, not the panel above it that happens to be closer.
  const onRow = items.map((_, i) => i).filter((i) => items[i].top <= y && y <= items[i].bottom);
  const pool = onRow.length ? onRow : items.map((_, i) => i);
  let best = pool[0];
  for (const i of pool) if (dist(items[i]) < dist(items[best])) best = i;
  const r = items[best];
  if (x < (r.left + r.right) / 2) return r.key;
  return items.slice(best + 1).find((i) => i.key !== r.key)?.key ?? null;
}
