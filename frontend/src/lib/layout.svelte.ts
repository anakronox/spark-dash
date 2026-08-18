/** Layout preferences — section order, visibility, and node card density.
 *
 * Named Layout rather than Sections because it owns how the page is arranged,
 * not just the stack below the node cards.
 *
 * localStorage rather than server-side: the backend is deliberately stateless,
 * and a layout tuned for a 34" monitor is rarely the one you want on a phone.
 * Per-browser is the behaviour you'd actually want here, not a limitation.
 *
 * Order, collapse and visibility live under separate keys so that a corrupt or
 * outdated value for one can't take the others down with it.
 *
 * COLLAPSED AND HIDDEN ARE DIFFERENT THINGS. A collapsed section is still on
 * the page as a named bar, one click from coming back — that control lives on
 * the section itself. A hidden one is not rendered at all, so nothing on the
 * page can bring it back; that is precisely why it is toggled from settings,
 * which is the only place it can be found again.
 */

import { columnStore } from './columns.svelte';

const STORAGE_KEY = 'spark-dash.section-order.v1';
const COLLAPSE_KEY = 'spark-dash.section-collapsed.v1';
const HIDDEN_KEY = 'spark-dash.section-hidden.v1';
const COMPACT_KEY = 'spark-dash.compact-cards.v1';
const ROWS_KEY = 'spark-dash.section-rows.v1';
const COLUMN_KEY = 'spark-dash.section-column.v1';
const WIDTH_KEY = 'spark-dash.section-widths.v1';
const PLACEMENT_KEY = 'spark-dash.section-placement.v1';

/** Which of the page's three zones a section sits in.
 *
 * WHY ZONES REPLACED A WIDTH FLAG. The previous model was one ordered list
 * plus half/full, rendered as a two-column CSS grid. That model cannot express
 * the thing this layout is for: a grid packs by ROWS, and a row is as tall as
 * its tallest item, so a short section beside a tall one leaves dead space that
 * nothing can occupy. Measured on the real dashboard, `models` (337px) beside
 * `processes` (661px) left 324px unusable — and `activity` (167px) would have
 * fitted in it twice over.
 *
 * Columns that fill INDEPENDENTLY are the only fix, and independent columns
 * have to be separate elements: there is no row for their contents to align to.
 * So a section is in the left column, the right column, or the full-width band.
 *
 * THE BAND IS ABOVE THE COLUMNS, and that is a real constraint rather than an
 * oversight. A full-width section cannot sit BETWEEN column content, because
 * two independently-filling columns have no shared horizontal line for it to
 * interrupt. Wide things — the history chart — go at the top, which is where
 * they belonged anyway.
 *
 * Two columns rather than an arbitrary number, deliberately. These sections are
 * wide data tables; at three across the columns collide and the history plot
 * loses the time resolution that makes it worth having.
 */
export type Zone = 'full' | 'left' | 'right';

export const ZONES: Zone[] = ['full', 'left', 'right'];

export const ZONE_LABEL: Record<Zone, string> = {
  full: 'full width',
  left: 'left column',
  right: 'right column',
};

function isZone(v: unknown): v is Zone {
  return v === 'full' || v === 'left' || v === 'right';
}

/** Placement, migrating a saved half/full width if that is all there is.
 *
 * Without the migration, everyone who had already arranged a dashboard would
 * open this release to a single full-width stack. Halves alternate into the two
 * columns in their existing order, which is the closest thing to what the grid
 * was showing them.
 */
function readPlacement(available: string[] = DEFAULT_ORDER): Record<string, Zone> {
  const known = new Set(available);
  try {
    const saved = JSON.parse(localStorage.getItem(PLACEMENT_KEY) ?? 'null');
    if (saved && typeof saved === 'object') {
      const out: Record<string, Zone> = {};
      for (const [id, z] of Object.entries(saved)) {
        if (known.has(id) && isZone(z)) out[id] = z;
      }
      return out;
    }
  } catch {
    return {};
  }

  try {
    const widths = JSON.parse(localStorage.getItem(WIDTH_KEY) ?? 'null');
    if (!widths || typeof widths !== 'object') return {};
    const out: Record<string, Zone> = {};
    let nextHalf: Zone = 'left';
    for (const id of available) {
      const w = (widths as Record<string, unknown>)[id];
      if (w === 'full') out[id] = 'full';
      else if (w === 'half') {
        out[id] = nextHalf;
        nextHalf = nextHalf === 'left' ? 'right' : 'left';
      }
    }
    return out;
  } catch {
    return {};
  }
}

function readCompact(): boolean {
  try {
    return localStorage.getItem(COMPACT_KEY) === '1';
  } catch {
    return false;
  }
}

export interface SectionDef {
  id: string;
  /** Shown in the drag handle's accessible name and its tooltip. */
  label: string;
}

/** Default order, top to bottom.
 *
 * Trend first, then what's consuming the box right now, with the model roster
 * and the swap timeline below. The node cards above already answer "what is
 * happening this second", so the section under them is more useful showing
 * where things have been heading than repeating the live state.
 *
 * Only affects browsers with no saved order: `reconcile` keeps an existing
 * one, so anyone who has already reordered (or simply visited before) keeps
 * what they had until they use "reset layout".
 */
export const SECTIONS: SectionDef[] = [
  { id: 'history', label: 'History' },
  { id: 'processes', label: 'GPU processes' },
  { id: 'network', label: 'Network' },
  { id: 'models', label: 'Models' },
  { id: 'activity', label: 'Model activity' },
];

const DEFAULT_ORDER = SECTIONS.map((s) => s.id);

/** Sections whose body is a list of rows, and can therefore be capped.
 *
 * History is absent because it is a chart: its height is set by the plot, not
 * by a row count, and a "max rows" control on it would be a setting that does
 * nothing.
 */
export const PAGED_SECTIONS = new Set(['processes', 'models', 'network', 'activity']);

/** Row caps offered in settings. `0` means uncapped.
 *
 * A sentinel rather than Infinity because this round-trips through JSON, where
 * `Infinity` serialises to `null` and comes back as a broken value. Translated
 * at the single point of use in `rowsFor`.
 */
export const ROW_CHOICES = [5, 8, 10, 15, 25, 50, 0];

/** Per section, because the sections are not alike.
 *
 * Network is lower because it draws TWO tables — RDMA ports and interfaces —
 * and the cap applies to each, so 8 there is 16 rows of section against 10 for
 * a table that draws one.
 */
const DEFAULT_ROWS: Record<string, number> = {
  processes: 10,
  models: 10,
  network: 8,
  activity: 10,
};

function readColumns(available: string[] = DEFAULT_ORDER): Record<string, Zone> {
  try {
    const saved = JSON.parse(localStorage.getItem(COLUMN_KEY) ?? 'null');
    if (!saved || typeof saved !== 'object') return {};
    const known = new Set(available);
    const out: Record<string, Zone> = {};
    for (const [id, z] of Object.entries(saved)) {
      if (known.has(id) && (z === 'left' || z === 'right')) out[id] = z;
    }
    return out;
  } catch {
    return {};
  }
}


function readRows(available: string[] = DEFAULT_ORDER): Record<string, number> {
  try {
    const saved = JSON.parse(localStorage.getItem(ROWS_KEY) ?? 'null');
    if (!saved || typeof saved !== 'object') return {};
    const known = new Set(available);
    const out: Record<string, number> = {};
    for (const [id, n] of Object.entries(saved)) {
      // Validated against the offered choices rather than merely "is a number":
      // a hand-edited 100000 would silently defeat the whole point of the cap,
      // and a negative would render nothing at all.
      if (known.has(id) && typeof n === 'number' && ROW_CHOICES.includes(n)) out[id] = n;
    }
    return out;
  } catch {
    return {};
  }
}

/** Reconcile a saved order against the sections that currently exist.
 *
 * Both directions matter and both are silent failures otherwise: a section
 * added in a later release must appear rather than being dropped because an
 * old saved order didn't mention it, and a section since removed must not
 * leave a hole. So: keep known ids in their saved order, then append anything
 * new in its default position.
 */
export function reconcile(saved: unknown, available: string[] = DEFAULT_ORDER): string[] {
  const known = new Set(available);
  const fromSaved = Array.isArray(saved)
    ? saved.filter((id): id is string => typeof id === 'string' && known.has(id))
    : [];

  const seen = new Set(fromSaved);
  const appended = available.filter((id) => !seen.has(id));
  return [...fromSaved, ...appended];
}

function read(): string[] {
  try {
    return reconcile(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? 'null'));
  } catch {
    // Corrupt or unavailable storage (private mode, quota) shouldn't stop the
    // dashboard rendering — fall back to the default order.
    return [...DEFAULT_ORDER];
  }
}

/** A saved list of section ids, filtered to ones that still exist.
 *
 * Unknown ids are dropped rather than kept: a section removed in a later
 * release would otherwise leave an entry that can never be cleared, and
 * `isDefault` would report a customised layout forever. For the hidden list
 * that failure is worse than untidy — a stale id would hide a section with no
 * way to bring it back.
 */
function readIdList(key: string, available: string[] = DEFAULT_ORDER): string[] {
  try {
    const saved = JSON.parse(localStorage.getItem(key) ?? 'null');
    if (!Array.isArray(saved)) return [];
    const known = new Set(available);
    return saved.filter((id): id is string => typeof id === 'string' && known.has(id));
  } catch {
    return [];
  }
}

function readCollapsed(available: string[] = DEFAULT_ORDER): string[] {
  return readIdList(COLLAPSE_KEY, available);
}

export class Layout {
  order = $state<string[]>(read());
  /** Section ids currently collapsed — present on the page, but folded. */
  collapsed = $state<string[]>(readCollapsed());
  /** Section ids not rendered at all. Recoverable only from settings. */
  hidden = $state<string[]>(readIdList(HIDDEN_KEY));

  /* Node cards reduced to name, status and the memory band.
   *
   * A DELIBERATE CHOICE, never automatic. Switching on node count is tempting
   * — the stack is 147px per card, so eight nodes push everything else below
   * the fold — but a page that rearranges itself when a node joins is
   * disorienting, and a node joining is exactly when someone is watching.
   * Default off; the person who needs it turns it on and it stays on. */
  compactCards = $state<boolean>(readCompact());

  /* Which zone each section sits in. Absent means full width, so a section
     added in a later release spans the page rather than silently appearing in
     a column the reader may have scrolled past. */
  placement = $state<Record<string, Zone>>(readPlacement());

  /** Rows a section shows before it pages. Absent means the section's own
   *  default; `0` means uncapped. */
  rows = $state<Record<string, number>>(readRows());

  /** The column each section was last in, so `half` can return it there. */
  lastColumn = $state<Record<string, Zone>>(readColumns());

  /** Id of the section being dragged, or null. Drives the lift and dims the
   *  card it came from. An id rather than an index: the section keeps its
   *  place in the layout for the whole drag now, so there is no index to
   *  track. */
  dragId = $state<string | null>(null);

  /** Where a release would put it: the zone, the position within that zone,
   *  and the y offset (relative to the zone's box) to draw the line at.
   *
   * NOTHING MOVES UNTIL THE POINTER IS RELEASED. The previous version reordered
   * live on every crossing, which meant the layout was rearranging underneath
   * the thing you were aiming at, and the card had to be re-anchored after each
   * swap to stop it jumping. Showing the destination instead of performing it
   * is both calmer to use and drastically simpler: no compensation, no
   * animation bookkeeping, and no way for a reorder to feed back into the
   * targeting that caused it. */
  drop = $state<{ zone: Zone; index: number; y: number } | null>(null);

  #save() {
    this.commit();
  }

  /** Write the order out now, whatever the drag state. */
  commit() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(this.order));
    } catch {
      // Not worth surfacing: reordering still works for this session.
    }
  }

  #saveCollapsed() {
    try {
      localStorage.setItem(COLLAPSE_KEY, JSON.stringify(this.collapsed));
    } catch {
      // Same reasoning as #save: the toggle still works for this session.
    }
  }

  isCollapsed(id: string): boolean {
    return this.collapsed.includes(id);
  }

  toggleCollapsed(id: string) {
    this.collapsed = this.isCollapsed(id)
      ? this.collapsed.filter((s) => s !== id)
      : [...this.collapsed, id];
    this.#saveCollapsed();
  }

  setCompactCards(on: boolean) {
    this.compactCards = on;
    try {
      localStorage.setItem(COMPACT_KEY, on ? '1' : '0');
    } catch {
      // Still applied for this session.
    }
  }

  zoneOf(id: string): Zone {
    return this.placement[id] ?? 'full';
  }

  /** The row cap a section's tables should use.
   *
   * `Infinity` for uncapped, which is what the table code wants: it makes the
   * page arithmetic degenerate correctly — one page, everything on it, and the
   * pager hides itself because the row count is never greater than the cap.
   */
  rowsFor(id: string): number {
    const n = this.rows[id] ?? DEFAULT_ROWS[id] ?? 10;
    return n === 0 ? Infinity : n;
  }

  /** The stored value, for settings to display. Distinct from `rowsFor`
   *  because 0 must read as "all" there, not as Infinity. */
  rowChoice(id: string): number {
    return this.rows[id] ?? DEFAULT_ROWS[id] ?? 10;
  }

  setRows(id: string, n: number) {
    this.rows = { ...this.rows, [id]: n };
    try {
      localStorage.setItem(ROWS_KEY, JSON.stringify(this.rows));
    } catch {
      // Still applied for this session.
    }
  }

  #savePlacement() {
    try {
      localStorage.setItem(PLACEMENT_KEY, JSON.stringify(this.placement));
    } catch {
      // Still applied for this session.
    }
  }

  /** The visible sections of one zone, in order.
   *
   * Derived from the single `order` array rather than stored per zone. Because
   * filtering preserves relative order, each zone's sequence is independent for
   * free — reordering the right column cannot disturb the left — while there
   * is still only one list to reconcile against new or removed sections.
   */
  inZone(zone: Zone): string[] {
    return this.visible.filter((id) => this.zoneOf(id) === zone);
  }

  /** Put a section in a zone at a position, where `index` counts the OTHER
   *  sections already in that zone.
   *
   * Counting others rather than the zone as it currently reads is what makes a
   * drag within one column work: the section being moved is still sitting in
   * that column while you aim, and an index that included it would be off by
   * one for every destination below its current home.
   */
  /** Change a section's zone WITHOUT touching the order.
   *
   * This is what makes full <-> half reversible, and it needs no extra state.
   * `order` is one list for the whole page; a zone's contents are that list
   * filtered by `placement`, so a section's position among its column-mates is
   * already recorded there. Leave `order` alone and a section sent to the
   * full-width band and back lands exactly where it was.
   *
   * `place()` below cannot do this — a drag has to say WHERE in the target
   * column the section goes, so it rewrites the order on purpose. The settings
   * toggle has no such opinion, and taking one was the bug: it appended to the
   * end of the target zone, so a round trip through full/left/right silently
   * moved a section to the bottom of the column it started at the top of.
   */
  setZone(id: string, zone: Zone) {
    if (zone !== 'full') this.#rememberColumn(id, zone);
    this.placement = { ...this.placement, [id]: zone };
    this.#savePlacement();
  }

  /** Which column a section was last in, so `half` can put it back.
   *
   * Without it, going full and back would have to guess a column, and guessing
   * the emptier one means a section you deliberately put on the right can
   * silently reappear on the left. Only consulted for a section that has never
   * been in a column at all.
   */
  #rememberColumn(id: string, zone: Zone) {
    if (this.lastColumn[id] === zone) return;
    this.lastColumn = { ...this.lastColumn, [id]: zone };
    try {
      localStorage.setItem(COLUMN_KEY, JSON.stringify(this.lastColumn));
    } catch {
      // Still applied for this session.
    }
  }

  /** Full width, or in a column. TWO states, because the third was unaimable.
   *
   * The panel used to cycle full -> left -> right, and with no natural order
   * among three zones every click's destination had to be memorised rather
   * than predicted. Settings answers the coarse question — wide or narrow —
   * which is the part you can decide without looking at the page. WHICH column
   * and where in it is a question you can only answer with the page in front
   * of you, so it belongs to the drag.
   */
  toggleWidth(id: string) {
    if (this.zoneOf(id) === 'full') {
      this.setZone(id, this.lastColumn[id] ?? this.#emptierColumn());
    } else {
      this.setZone(id, 'full');
    }
  }

  /** For a section that has never been in a column. */
  #emptierColumn(): Zone {
    return this.inZone('left').length <= this.inZone('right').length ? 'left' : 'right';
  }

  place(id: string, zone: Zone, index: number) {
    if (zone !== 'full') this.#rememberColumn(id, zone);
    const others = this.inZone(zone).filter((x) => x !== id);
    const next = this.order.filter((x) => x !== id);

    let at: number;
    if (others.length === 0) at = next.length;
    else if (index >= others.length) at = next.indexOf(others[others.length - 1]) + 1;
    else at = next.indexOf(others[index]);

    next.splice(at, 0, id);
    this.order = next;
    this.placement = { ...this.placement, [id]: zone };
    this.#savePlacement();
    this.#save();
  }

  /** Move a section up or down within its own zone. */
  moveInZone(id: string, delta: number) {
    const zone = this.zoneOf(id);
    const list = this.inZone(zone);
    const i = list.indexOf(id);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= list.length) return;
    this.place(id, zone, j);
  }

  /** Move a section to the neighbouring zone, keeping it at the end.
   *
   * The keyboard counterpart of dragging across. Appending rather than trying
   * to preserve a position: the two columns fill independently, so "the same
   * place" in another column is not a meaningful spot, and the end is the one
   * position that always exists.
   */
  shiftZone(id: string, delta: number) {
    const i = ZONES.indexOf(this.zoneOf(id));
    const j = i + delta;
    if (j < 0 || j >= ZONES.length) return;
    const zone = ZONES[j];
    this.place(id, zone, this.inZone(zone).filter((x) => x !== id).length);
  }

  isHidden(id: string): boolean {
    return this.hidden.includes(id);
  }

  /** Take a section off the dashboard entirely, or put it back.
   *
   * Collapse state is left untouched rather than cleared: hiding is not a
   * stronger collapse, it is a different axis. Something you folded away and
   * then hid should come back folded, not sprung open.
   */
  toggleHidden(id: string) {
    this.hidden = this.isHidden(id)
      ? this.hidden.filter((s) => s !== id)
      : [...this.hidden, id];
    try {
      localStorage.setItem(HIDDEN_KEY, JSON.stringify(this.hidden));
    } catch {
      // Still applied for this session.
    }
  }

  /** The order with hidden sections removed — what the page actually renders.
   *
   * Indices come from THIS list, not from `order`, because Section uses its
   * index for drag arithmetic and the accessible "3 of 5" position. Passing an
   * index from the unfiltered list would make a hidden section above shift
   * every position below it out of step with what is on screen.
   */
  get visible(): string[] {
    return this.order.filter((id) => !this.isHidden(id));
  }

  move(from: number, to: number) {
    if (from === to || from < 0 || to < 0) return;
    if (from >= this.order.length || to >= this.order.length) return;

    const next = [...this.order];
    const [moved] = next.splice(from, 1);
    next.splice(to, 0, moved);
    this.order = next;
    this.#save();
  }

  reset() {
    this.order = [...DEFAULT_ORDER];
    this.collapsed = [];
    this.hidden = [];
    this.placement = {};
    this.lastColumn = {};
    this.rows = {};
    /* Switched-off columns go too. Same unrecoverability rule as hidden
       sections: anything that can remove a thing from the page must have one
       control that puts everything back, or a reader who forgets what they hid
       is stuck with a table they cannot explain. */
    columnStore.reset();
    this.setCompactCards(false);
    this.#savePlacement();
    try {
      localStorage.removeItem(COLUMN_KEY);
      localStorage.removeItem(ROWS_KEY);
    } catch {
      // Still applied for this session.
    }
    try {
      // Cleared as well as superseded: readPlacement falls back to the old
      // width key when there is no placement, so leaving a stale one behind
      // would resurrect the previous arrangement on the next load.
      localStorage.removeItem(WIDTH_KEY);
    } catch {
      // Still applied for this session.
    }
    try {
      localStorage.setItem(HIDDEN_KEY, JSON.stringify(this.hidden));
    } catch {
      // Still applied for this session.
    }
    this.#save();
    this.#saveCollapsed();
  }

  /** True when nothing has been customised — order untouched, nothing
   *  collapsed AND nothing hidden. Drives whether "reset layout" is offered at
   *  all, so it has to account for all three or a customised dashboard has no
   *  way back. */
  get isDefault(): boolean {
    return (
      this.order.join(',') === DEFAULT_ORDER.join(',') &&
      this.collapsed.length === 0 &&
      this.hidden.length === 0 &&
      Object.keys(this.placement).length === 0 &&
      Object.keys(this.rows).length === 0 &&
      !columnStore.customised &&
      !this.compactCards
    );
  }

  label(id: string): string {
    return SECTIONS.find((s) => s.id === id)?.label ?? id;
  }
}
