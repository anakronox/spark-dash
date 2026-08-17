/** Sorting and pagination for the data tables.
 *
 * WHY BOTH, AND WHY TOGETHER. Either alone is half a solution. Sorting without
 * a page limit still renders every row, so the section keeps growing as nodes
 * are added — at four nodes the models table is already 36 rows, of which 32
 * are `unloaded`, and the same ratio at 32 nodes is ~288. Paginating without
 * sorting just hides rows behind a page control, and the one you wanted is as
 * likely to be on page 9 as page 1.
 *
 * Together they do what neither does: the sort decides what "interesting"
 * means, and the page limit means the section costs a fixed amount of screen
 * no matter how large the cluster gets.
 *
 * THE DEFAULT SORT IS NOT "UNSORTED". Each table already had a deliberate
 * order — models lead with what is serving, processes with the biggest
 * consumer — and that order IS the default here, reachable again by cycling
 * past descending. A sort control that cannot return to the view the table was
 * designed around would throw away the reasoning behind it.
 */

export type SortDir = 'asc' | 'desc';

/** What a header cell needs from a table view in order to sort it.
 *
 * Narrower than TableView on purpose: it drops the row type, so SortButton can
 * be handed the view for a table of processes or of RDMA ports without being
 * made generic over something it never touches. TableView<T> satisfies it
 * structurally for every T.
 */
export interface SortControl {
  readonly sortKey: string | null;
  readonly dir: SortDir;
  toggle(key: string): void;
  ariaSort(key: string): 'ascending' | 'descending' | 'none';
}

/** What a pager needs from a table view. Row-type-free for the same reason as
 *  SortControl. */
export interface PageControl {
  readonly pageSize: number;
  range(total: number): string;
  current(total: number): number;
  pageCount(total: number): number;
  go(delta: number, total: number): void;
}

/** A sortable column: the id TableView knows it by, and how it's headed.
 *
 * Declaring both together is what stops a header sorting by the column beside
 * it — a failure that reads as the DATA being wrong rather than the header, so
 * it is worth making structurally impossible rather than merely testing for.
 */
export interface ColumnDef {
  key: string;
  label: string;
  /** Numeric column: right-aligned and shrunk to its content. */
  right?: boolean;
  /** Extra class for the header cell, where a column needs its own width. */
  cls?: string;
}

export interface ColumnSort<T> {
  /** Stable id, also what `aria-sort` is keyed on. */
  key: string;
  /** Value to order by. Null sorts last in both directions — a missing
   *  reading is not a small one, and letting nulls lead an ascending sort
   *  fills the first page with rows that have nothing to say. */
  value: (row: T) => string | number | null;
}

export class TableView<T> {
  /** null = the table's own deliberate order. */
  sortKey = $state<string | null>(null);
  dir = $state<SortDir>('desc');
  page = $state(0);
  pageSize = $state(10);

  #columns: Map<string, ColumnSort<T>>;

  constructor(columns: ColumnSort<T>[], pageSize = 10) {
    this.#columns = new Map(columns.map((c) => [c.key, c]));
    this.pageSize = pageSize;
  }

  /** Cycle: descending -> ascending -> back to the table's own order.
   *
   * Descending first because every numeric column here is one where "most" is
   * the interesting end — highest throughput, biggest memory, most errors.
   * Ascending-first would make the first click show the least interesting
   * rows.
   */
  toggle(key: string) {
    if (this.sortKey !== key) {
      this.sortKey = key;
      this.dir = 'desc';
    } else if (this.dir === 'desc') {
      this.dir = 'asc';
    } else {
      this.sortKey = null;
    }
    this.page = 0;
  }

  /** For `aria-sort` on the header cell. */
  ariaSort(key: string): 'ascending' | 'descending' | 'none' {
    if (this.sortKey !== key) return 'none';
    return this.dir === 'asc' ? 'ascending' : 'descending';
  }

  sorted(rows: T[]): T[] {
    const col = this.sortKey ? this.#columns.get(this.sortKey) : undefined;
    if (!col) return rows;

    const sign = this.dir === 'asc' ? 1 : -1;
    // Copied before sorting: these arrays come from $derived state and sorting
    // in place would mutate the source.
    return [...rows].sort((a, b) => {
      const av = col.value(a);
      const bv = col.value(b);
      if (av === null && bv === null) return 0;
      if (av === null) return 1; // nulls last, both directions
      if (bv === null) return -1;
      if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * sign;
      return String(av).localeCompare(String(bv)) * sign;
    });
  }

  pageCount(total: number): number {
    return Math.max(1, Math.ceil(total / this.pageSize));
  }

  /** Clamped, because the row count moves under us: a node going away can
   *  shrink the table while you are on its last page, and being stranded on an
   *  empty page reads as broken data rather than a vanished row. */
  current(total: number): number {
    return Math.min(this.page, this.pageCount(total) - 1);
  }

  /** Index of the first row on the current page.
   *
   * The finiteness guard is not defensive padding. An uncapped table sets
   * `pageSize` to Infinity, and the first page then starts at
   * `0 * Infinity`, which is NaN — and `slice(NaN, NaN)` returns NOTHING.
   * "Show me every row" rendering an empty table is the worst possible way for
   * that to fail, and it is one multiplication away at all times.
   */
  #start(total: number): number {
    if (!Number.isFinite(this.pageSize)) return 0;
    return this.current(total) * this.pageSize;
  }

  slice(rows: T[]): T[] {
    const sorted = this.sorted(rows);
    if (!Number.isFinite(this.pageSize)) return sorted;
    const start = this.#start(sorted.length);
    return sorted.slice(start, start + this.pageSize);
  }

  /** "11–20 of 288" — the range, not just the page number, because the
   *  question being answered is "how much am I not looking at". */
  range(total: number): string {
    if (!total) return '0';
    const start = this.#start(total);
    return `${start + 1}–${Math.min(start + this.pageSize, total)} of ${total}`;
  }

  go(delta: number, total: number) {
    this.page = Math.max(0, Math.min(this.current(total) + delta, this.pageCount(total) - 1));
  }
}
