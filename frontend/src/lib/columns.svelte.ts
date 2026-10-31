/** Which columns of a table are shown, and which the reader has switched off.
 *
 * COLUMNS, NOT ROWS. This hides the columns of a table. Filtering the rows
 * ("only gx10-b") is a different feature with a different control — which is
 * why nothing here wears a funnel glyph: a funnel conventionally means row
 * filtering, and spending it on columns would leave that feature without its
 * obvious icon.
 *
 * AN OVERRIDE ON A CURATED DEFAULT, never a build-your-own table. Every column
 * in these tables earned its place and most carry a comment saying why; the
 * shipped set stays the considered one and hiding is opt-in, per browser.
 *
 * localStorage rather than server-side, like every other view preference here:
 * the backend is deliberately stateless, and a column set tuned for a 34"
 * monitor is not the one you want on a phone.
 */

import { applyOrder, moveBefore } from './column-order';
import type { ColumnDef } from './table.svelte';

const KEY = 'spark-dash.section-columns.v1';
const WIDTH_KEY = 'spark-dash.column-widths.v1';
const ORDER_KEY = 'spark-dash.column-order.v1';

/** Below this a column is unreadable and, worse, its drag handle is hard to
 *  grab again — so a width dragged to nothing has no easy way back. Enforced
 *  in the store rather than only in the drag handler, because it also bounds
 *  whatever a previous browser wrote. */
export const MIN_COLUMN_PX = 44;

/** No column needs more than this, and a stored value beyond it is almost
 *  always a drag on a much wider monitor. Clamped rather than dropped: the
 *  reader still gets a wide column, just not one that pushes everything else
 *  off the page. */
const MAX_COLUMN_PX = 900;

function readWidths(): Record<string, Record<string, number>> {
  try {
    const saved = JSON.parse(localStorage.getItem(WIDTH_KEY) ?? 'null');
    if (!saved || typeof saved !== 'object' || Array.isArray(saved)) return {};
    const out: Record<string, Record<string, number>> = {};
    for (const [table, widths] of Object.entries(saved)) {
      if (!widths || typeof widths !== 'object' || Array.isArray(widths)) continue;
      const clean: Record<string, number> = {};
      for (const [key, px] of Object.entries(widths as Record<string, unknown>)) {
        // CLAMPED ON READ, not on write. A width dragged on a 2560px monitor is
        // nonsense on a 1280px laptop and the same browser opens both, so the
        // stored value cannot be trusted at the point of use — the same lesson
        // the pager learned when the row count moved underneath its index.
        if (typeof px === 'number' && Number.isFinite(px)) {
          clean[key] = Math.min(MAX_COLUMN_PX, Math.max(MIN_COLUMN_PX, Math.round(px)));
        }
      }
      if (Object.keys(clean).length) out[table] = clean;
    }
    return out;
  } catch {
    // Same failure direction as the hidden-column store: a table that renders
    // at its default widths is fine; one that does not render is not.
    return {};
  }
}

function read(key = KEY): Record<string, string[]> {
  try {
    const saved = JSON.parse(localStorage.getItem(key) ?? 'null');
    if (!saved || typeof saved !== 'object' || Array.isArray(saved)) return {};
    const out: Record<string, string[]> = {};
    for (const [table, keys] of Object.entries(saved)) {
      // Unknown column ids are dropped by ColumnView rather than here, because
      // only it knows which columns its table actually has. What matters at
      // this level is that the shape is right.
      if (Array.isArray(keys)) out[table] = keys.filter((k): k is string => typeof k === 'string');
    }
    return out;
  } catch {
    // Corrupt or unavailable storage shouldn't stop a table rendering — every
    // column simply shows, which is the safe direction to fail in.
    return {};
  }
}

/** One reactive record for every table on the page.
 *
 * Shared rather than per-instance so that "reset layout" can clear the lot and
 * have every table update. Per-instance state with a storage write would leave
 * the live views showing what was just reset.
 */
class ColumnStore {
  hidden = $state<Record<string, string[]>>(read());

  /** table -> column key -> width in PIXELS.
   *
   * Pixels rather than a fraction of the container, which is the obvious
   * alternative and is worse: hiding a column changes what the container's
   * width means, so stored fractions drift every time the reader toggles one.
   * Pixels stay literal and are clamped where they are read.
   */
  widths = $state<Record<string, Record<string, number>>>(readWidths());

  /** table -> the movable columns' keys, in the reader's order (CO). Unknown
   *  keys are dropped where a view applies it, as for `hidden`. */
  order = $state<Record<string, string[]>>(read(ORDER_KEY));

  #save() {
    try {
      localStorage.setItem(KEY, JSON.stringify(this.hidden));
    } catch {
      // Still applied for this session.
    }
  }

  #saveWidths() {
    try {
      localStorage.setItem(WIDTH_KEY, JSON.stringify(this.widths));
    } catch {
      // Still applied for this session.
    }
  }

  #saveOrder() {
    try {
      localStorage.setItem(ORDER_KEY, JSON.stringify(this.order));
    } catch {
      // Still applied for this session.
    }
  }

  getOrder(table: string): string[] {
    return this.order[table] ?? [];
  }

  setOrder(table: string, keys: string[]) {
    this.order = { ...this.order, [table]: keys };
    this.#saveOrder();
  }

  get(table: string): string[] {
    return this.hidden[table] ?? [];
  }

  set(table: string, keys: string[]) {
    this.hidden = { ...this.hidden, [table]: keys };
    this.#save();
  }

  width(table: string, key: string): number | null {
    return this.widths[table]?.[key] ?? null;
  }

  setWidth(table: string, key: string, px: number) {
    const clamped = Math.min(MAX_COLUMN_PX, Math.max(MIN_COLUMN_PX, Math.round(px)));
    this.widths = {
      ...this.widths,
      [table]: { ...(this.widths[table] ?? {}), [key]: clamped },
    };
    this.#saveWidths();
  }

  /** Back to the ColumnDef default for one column. The escape from a column
   *  dragged too narrow to grab again — which is why it exists at all. */
  clearWidth(table: string, key: string) {
    const current = this.widths[table];
    if (!current || !(key in current)) return;
    const { [key]: _dropped, ...rest } = current;
    this.widths = { ...this.widths, [table]: rest };
    this.#saveWidths();
  }

  /** RESETS ALL THREE. A column dragged to its minimum is hidden in every
   *  sense that matters, so a reset that restored visibility but not width
   *  would leave the reader stuck with the half they could not see. Order
   *  goes with them: a reset that left columns moved is not a reset. */
  reset() {
    this.hidden = {};
    this.widths = {};
    this.order = {};
    try {
      localStorage.removeItem(KEY);
      localStorage.removeItem(WIDTH_KEY);
      localStorage.removeItem(ORDER_KEY);
    } catch {
      // Still applied for this session.
    }
  }

  get customised(): boolean {
    return (
      Object.values(this.hidden).some((keys) => keys.length > 0) ||
      Object.values(this.widths).some((w) => Object.keys(w).length > 0) ||
      Object.values(this.order).some((keys) => keys.length > 0)
    );
  }
}

export const columnStore = new ColumnStore();

export class ColumnView {
  #table: string;
  #columns: ColumnDef[];

  /** Columns that have something to say right now — `err` or `drop` above
   *  zero. Deliberately NOT persisted — this is a fact about the current data,
   *  not a preference. */
  flagged = $state<string[]>([]);

  constructor(table: string, columns: ColumnDef[]) {
    this.#table = table;
    this.#columns = columns;
  }

  get columns(): ColumnDef[] {
    return this.#columns;
  }

  /** What the reader chose, ignoring whether anything is currently forcing it
   *  back into view. This is what the menu's checkbox reflects — a switch that
   *  flipped itself because of an alert would be a switch you cannot trust. */
  isOff(key: string): boolean {
    const stored = columnStore.get(this.#table);
    return this.#optional(key) ? !stored.includes(`+${key}`) : stored.includes(key);
  }

  #optional(key: string): boolean {
    return this.#columns.find((c) => c.key === key)?.optional ?? false;
  }

  /** Switched off while it has something to say. It STAYS off (BZ); the menu
   *  says so beside the unticked box, which is all the reader agreed to. */
  isFlagged(key: string): boolean {
    return this.isOff(key) && this.flagged.includes(key);
  }

  /** Every column, in the reader's order: required ones first. */
  ordered(): ColumnDef[] {
    return applyOrder(this.#columns, columnStore.getOrder(this.#table));
  }

  /** What actually renders, in the reader's order. Headers and cells both
   *  iterate this, which is why one reorder moves a whole column. */
  visible(): ColumnDef[] {
    return this.ordered().filter((c) => !this.isOff(c.key));
  }

  /** Move `key` in front of `before`, or to the end for null. Required
   *  columns never move and nothing moves in front of them. */
  move(key: string, before: string | null) {
    const movable = this.ordered().filter((c) => !c.required).map((c) => c.key);
    if (!movable.includes(key) || (before !== null && !movable.includes(before))) return;
    columnStore.setOrder(this.#table, moveBefore(movable, key, before));
  }

  /** One place left (-1) or right (+1) among the SHOWN columns: the keyboard's
   *  version of a drag, so a hidden column is never skipped over silently. */
  step(key: string, dir: -1 | 1) {
    const shown = this.visible().filter((c) => !c.required).map((c) => c.key);
    const i = shown.indexOf(key);
    if (i < 0) return;
    if (dir < 0 && i > 0) this.move(key, shown[i - 1]);
    else if (dir > 0 && i < shown.length - 1) this.move(key, shown[i + 2] ?? null);
  }

  /** Where `key` can go from here, for the menu's arrows. */
  canStep(key: string, dir: -1 | 1): boolean {
    const shown = this.visible().filter((c) => !c.required).map((c) => c.key);
    const i = shown.indexOf(key);
    return i >= 0 && (dir < 0 ? i > 0 : i < shown.length - 1);
  }

  get reordered(): boolean {
    return columnStore.getOrder(this.#table).length > 0;
  }

  isVisible(key: string): boolean {
    return !this.isOff(key);
  }

  toggle(key: string) {
    const col = this.#columns.find((c) => c.key === key);
    // Required columns are identity — a table of numbers with no idea which
    // node they belong to is unreadable, and it is exactly the mistake a
    // picker invites. Refused here rather than only hidden from the UI.
    if (!col || col.required) return;
    const current = columnStore.get(this.#table);
    const stored = col.optional ? `+${key}` : key;
    columnStore.set(
      this.#table,
      current.includes(stored) ? current.filter((k) => k !== stored) : [...current, stored],
    );
  }

  /** The width to render, or null to let the ColumnDef default stand.
   *
   * Null rather than a resolved number so the markup can emit the default as a
   * CSS unit (`ch`) and only switch to pixels once the reader has dragged.
   * `ch` is the right unit for a default — it tracks the font — and pixels are
   * the only honest unit for a drag, which happened at a specific size on a
   * specific screen. */
  width(key: string): number | null {
    return columnStore.width(this.#table, key);
  }

  setWidth(key: string, px: number) {
    columnStore.setWidth(this.#table, key, px);
  }

  resetWidth(key: string) {
    columnStore.clearWidth(this.#table, key);
  }

  get resized(): boolean {
    return this.#columns.some((c) => this.width(c.key) !== null);
  }

  get hiddenCount(): number {
    return this.#columns.filter((c) => this.isOff(c.key)).length;
  }

  /** Mark these columns as having something to say.
   *
   * For columns marked `signal` — `err` and `drop`. They read zero every day,
   * which is exactly why someone switches them off. Until BZ a non-zero value
   * forced the column back into view; now a column switched off stays off and
   * only the menu says it is not zero.
   *
   * The equality guard matters. This is driven from an $effect fed by derived
   * data, so assigning an equal-but-new array on every frame would re-run the
   * effect forever.
   */
  flag(keys: string[]) {
    if (keys.length === this.flagged.length && keys.every((k, i) => k === this.flagged[i])) return;
    this.flagged = keys;
  }
}
