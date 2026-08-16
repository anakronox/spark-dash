/** Section order and collapsed state, persisted per browser.
 *
 * localStorage rather than server-side: the backend is deliberately stateless,
 * and a layout tuned for a 34" monitor is rarely the one you want on a phone.
 * Per-browser is the behaviour you'd actually want here, not a limitation.
 *
 * Order and collapse live under separate keys so that a corrupt or outdated
 * value for one can't take the other down with it.
 */

const STORAGE_KEY = 'spark-dash.section-order.v1';
const COLLAPSE_KEY = 'spark-dash.section-collapsed.v1';

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

/** Collapsed section ids, filtered to ones that still exist.
 *
 * Unknown ids are dropped rather than kept: a section removed in a later
 * release would otherwise leave an entry that can never be un-collapsed,
 * and `isDefault` would report a customised layout forever.
 */
function readCollapsed(available: string[] = DEFAULT_ORDER): string[] {
  try {
    const saved = JSON.parse(localStorage.getItem(COLLAPSE_KEY) ?? 'null');
    if (!Array.isArray(saved)) return [];
    const known = new Set(available);
    return saved.filter((id): id is string => typeof id === 'string' && known.has(id));
  } catch {
    return [];
  }
}

export class Layout {
  order = $state<string[]>(read());
  /** Section ids currently collapsed. */
  collapsed = $state<string[]>(readCollapsed());
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
    this.#save();
    this.#saveCollapsed();
  }

  /** True when nothing has been customised — order untouched AND nothing
   *  collapsed. Drives whether "reset layout" is offered at all, so it has to
   *  account for both or a collapsed-but-unreordered dashboard would have no
   *  way back. */
  get isDefault(): boolean {
    return (
      this.order.join(',') === DEFAULT_ORDER.join(',') && this.collapsed.length === 0
    );
  }

  label(id: string): string {
    return SECTIONS.find((s) => s.id === id)?.label ?? id;
  }
}
