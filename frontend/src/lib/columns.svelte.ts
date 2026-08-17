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

import type { ColumnDef } from './table.svelte';

const KEY = 'spark-dash.section-columns.v1';

function read(): Record<string, string[]> {
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) ?? 'null');
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

  #save() {
    try {
      localStorage.setItem(KEY, JSON.stringify(this.hidden));
    } catch {
      // Still applied for this session.
    }
  }

  get(table: string): string[] {
    return this.hidden[table] ?? [];
  }

  set(table: string, keys: string[]) {
    this.hidden = { ...this.hidden, [table]: keys };
    this.#save();
  }

  reset() {
    this.hidden = {};
    try {
      localStorage.removeItem(KEY);
    } catch {
      // Still applied for this session.
    }
  }

  get customised(): boolean {
    return Object.values(this.hidden).some((keys) => keys.length > 0);
  }
}

export const columnStore = new ColumnStore();

export class ColumnView {
  #table: string;
  #columns: ColumnDef[];

  /** Columns shown despite being switched off, because they have something to
   *  say right now. Deliberately NOT persisted — this is a fact about the
   *  current data, not a preference. */
  forced = $state<string[]>([]);

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
    return columnStore.get(this.#table).includes(key);
  }

  /** Shown despite being switched off. The menu says so, because a column
   *  appearing on its own is otherwise indistinguishable from a bug. */
  isForced(key: string): boolean {
    return this.isOff(key) && this.forced.includes(key);
  }

  /** What actually renders. */
  visible(): ColumnDef[] {
    return this.#columns.filter((c) => !this.isOff(c.key) || this.forced.includes(c.key));
  }

  isVisible(key: string): boolean {
    return !this.isOff(key) || this.forced.includes(key);
  }

  toggle(key: string) {
    const col = this.#columns.find((c) => c.key === key);
    // Required columns are identity — a table of numbers with no idea which
    // node they belong to is unreadable, and it is exactly the mistake a
    // picker invites. Refused here rather than only hidden from the UI.
    if (!col || col.required) return;
    const current = columnStore.get(this.#table);
    columnStore.set(
      this.#table,
      current.includes(key) ? current.filter((k) => k !== key) : [...current, key],
    );
  }

  get hiddenCount(): number {
    return this.#columns.filter((c) => this.isOff(c.key)).length;
  }

  /** Force these columns back into view.
   *
   * For columns marked `signal` — `err` and `drop`. They read zero every day,
   * which is exactly why someone switches them off, and their first non-zero
   * value is the thing they needed to know. This is a monitoring dashboard:
   * hiding a stat is hiding a signal, and the signal wins.
   *
   * The equality guard matters. This is driven from an $effect fed by derived
   * data, so assigning an equal-but-new array on every frame would re-run the
   * effect forever.
   */
  force(keys: string[]) {
    if (keys.length === this.forced.length && keys.every((k, i) => k === this.forced[i])) return;
    this.forced = keys;
  }
}
