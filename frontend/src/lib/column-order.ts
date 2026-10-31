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
