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

const STORAGE_KEY = 'spark-dash.section-order.v1';
const COLLAPSE_KEY = 'spark-dash.section-collapsed.v1';
const HIDDEN_KEY = 'spark-dash.section-hidden.v1';
const COMPACT_KEY = 'spark-dash.compact-cards.v1';
const WIDTH_KEY = 'spark-dash.section-widths.v1';

/** Sections are laid out in a TWO-column grid. A section is either half — one
 *  column, sharing its row — or full, spanning both.
 *
 *  Two columns rather than an arbitrary number, deliberately. These sections
 *  are wide data tables and a chart; at three across, columns collide and the
 *  history plot loses the time resolution that makes it worth having. Two is
 *  the count where side-by-side is genuinely readable, so it is a constant
 *  rather than a setting. */
export type SectionWidth = 'half' | 'full';

function readWidths(available: string[] = DEFAULT_ORDER): Record<string, SectionWidth> {
  try {
    const saved = JSON.parse(localStorage.getItem(WIDTH_KEY) ?? 'null');
    if (!saved || typeof saved !== 'object') return {};
    const known = new Set(available);
    const out: Record<string, SectionWidth> = {};
    for (const [id, w] of Object.entries(saved)) {
      if (known.has(id) && (w === 'half' || w === 'full')) out[id] = w;
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

  /* Per-section width. Absent means full, so a section added in a later
     release shows at full width rather than silently half. */
  widths = $state<Record<string, SectionWidth>>(readWidths());
  /** Index currently being dragged, or null. Drives the visual lift. */
  dragging = $state<number | null>(null);

  #save() {
    // Skipped mid-drag. `move` is called on every swap, and localStorage
    // writes are synchronous — doing one per swap puts a blocking write in the
    // middle of an animation. The drag commits once when the pointer is
    // released; keyboard reordering isn't dragging, so it saves immediately.
    if (this.dragging !== null) return;
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

  widthOf(id: string): SectionWidth {
    return this.widths[id] ?? 'full';
  }

  setWidth(id: string, w: SectionWidth) {
    this.widths = { ...this.widths, [id]: w };
    try {
      localStorage.setItem(WIDTH_KEY, JSON.stringify(this.widths));
    } catch {
      // Still applied for this session.
    }
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

  /** Reorder by VISIBLE index, which is what the page renders and what a drag
   *  reports. Translating here rather than at the call site keeps the mapping
   *  in one place: with a section hidden, visible index 2 is not order index 2,
   *  and moving the wrong row is a silent, baffling bug.
   */
  moveVisible(from: number, to: number) {
    const vis = this.visible;
    if (from === to || from < 0 || to < 0) return;
    if (from >= vis.length || to >= vis.length) return;
    this.move(this.order.indexOf(vis[from]), this.order.indexOf(vis[to]));
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
    this.widths = {};
    this.setCompactCards(false);
    try {
      localStorage.setItem(WIDTH_KEY, JSON.stringify(this.widths));
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
      Object.keys(this.widths).length === 0 &&
      !this.compactCards
    );
  }

  label(id: string): string {
    return SECTIONS.find((s) => s.id === id)?.label ?? id;
  }
}
