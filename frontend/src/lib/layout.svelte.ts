/** Layout preferences — section order, visibility, and node card density.
 *
 * Named Layout rather than Sections because it owns how the page is arranged,
 * not just the stack below the node cards.
 *
 * localStorage rather than server-side: the backend is deliberately stateless,
 * and a layout tuned for a 34" monitor is rarely the one you want on a phone.
 * Per-browser is the behaviour you'd actually want here, not a limitation.
 *
 * Order and visibility live under separate keys so that a corrupt or
 * outdated value for one can't take the others down with it.
 *
 * HIDDEN keeps a card's place. A hidden card stays in `order` and its zone, so
 * showing it again puts it back where it was; that is what makes hide the
 * right meaning for the card's own close control, and why only COPIES are
 * removed outright (there is nothing of theirs worth keeping).
 */

import { columnStore } from './columns.svelte';

const STORAGE_KEY = 'spark-dash.section-order.v1';
const HIDDEN_KEY = 'spark-dash.section-hidden.v1';
const COMPACT_KEY = 'spark-dash.compact-cards.v1';
const PLOT_HEIGHT_KEY = 'spark-dash.plot-heights.v1';
const PLOT_ROWS_KEY = 'spark-dash.plot-rows.v1';
const OVERFLOW_KEY = 'spark-dash.overflow.v1';
const CARD_SPAN_KEY = 'spark-dash.card-spans.v1';
const ROWS_KEY = 'spark-dash.section-rows.v1';
const COLUMN_KEY = 'spark-dash.section-column.v1';
const WIDTH_KEY = 'spark-dash.section-widths.v1';
const PLACEMENT_KEY = 'spark-dash.section-placement.v1';
const NODE_ORDER_KEY = 'spark-dash.node-order.v1';

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

/** Where a node/cluster card sits, when the reader has said.
 *
 * KEYS ARE LIVE DATA, unlike the five section ids. A key is a cluster name or
 * a standalone node's id, so hardware appearing, disappearing or being
 * reclustered changes the set underneath a saved order. Reconciled on read
 * rather than trusted: unknown keys are dropped, and anything the saved list
 * has never seen is APPENDED in inventory order. Failing that way round means
 * a node added to cluster.yml shows up rather than being silently withheld
 * because a months-old ordering did not mention it.
 */
function readNodeOrder(): string[] {
  try {
    const saved = JSON.parse(localStorage.getItem(NODE_ORDER_KEY) ?? 'null');
    if (!Array.isArray(saved)) return [];
    return saved.filter((k): k is string => typeof k === 'string');
  } catch {
    // A page in inventory order is fine; a page that will not render is not.
    return [];
  }
}

/** One horizontal slice of the page.
 *
 * THE PAGE IS A SEQUENCE, not a full-width band above two columns. It was the
 * latter, and that structure could not express "a full-width card BELOW a
 * pair": every column card rendered under every full-width card, so sending a
 * card to a column dropped it to the bottom of the page no matter where its
 * order said it belonged. The order was right the whole time; there was
 * nowhere to draw it.
 *
 * A band is either one full-width card, or a maximal RUN of consecutive
 * column-placed cards sharing a left/right split. Deriving the run from the
 * existing order means nothing new is stored — the same `order` and
 * `placement` that were already saved now render in the right places.
 *
 * The columns still fill INDEPENDENTLY within a band, which is the property
 * that stops a short section stranding space beside a tall one. That was the
 * reason for the old structure and it survives; what changes is that the
 * stranding is now bounded by the band instead of applying to the whole page.
 */
export type Band =
  | { kind: 'full'; id: string }
  | {
      kind: 'cols';
      left: string[];
      right: string[];
      last: string;
    };

/** What a release would do. TWO SHAPES, because there are two gestures.
 *
 * `line` is the original: insert into a stack at an index, drawn as a
 * horizontal rule between cards.
 *
 * `pair` is the one aimed at the outer third of a full-width card. It turns
 * TWO cards into halves at once — the one being dragged and the one aimed at —
 * which no zone-plus-index can express, because it moves a card that is not
 * being dragged. Carrying the target's id is what makes that possible, and
 * carrying the rect is what lets the affordance be drawn at the size the card
 * will actually land at, rather than as a line saying "somewhere here".
 */
export type DropTarget =
  | {
      kind: 'line';
      zone: Zone;
      /** Which band's zone, since several zones now share a `zone` name. */
      band: number;
      /** The card the line is drawn against, and which side of it. An ANCHOR
       *  rather than an index: with the page a sequence of bands, an index
       *  within a zone no longer says where in the page-wide order the card
       *  goes, and an anchor says it exactly. Null only for an empty column. */
      anchorId: string | null;
      before: boolean;
      y: number;
    }
  | {
      kind: 'pair';
      zone: Zone;
      band: number;
      targetId: string;
      /** The column the DRAGGED card takes; the target takes the other. */
      side: 'left' | 'right';
      /** Relative to the zone's box, like `y` above. */
      rect: { x: number; y: number; w: number; h: number };
    };

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
        if (known.has(kindOf(id)) && isZone(z)) out[id] = z;
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

/** What a card does when its content is taller than it is. */
export type Overflow = 'page' | 'scroll';

function readOverflow(): Overflow {
  try {
    return localStorage.getItem(OVERFLOW_KEY) === 'scroll' ? 'scroll' : 'page';
  } catch {
    return 'page';
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
/* LABELS ARE NOT IDS. `history` shows as "System Activity" and
   `network-history` as "Network Activity" — renamed 2026-08-28, ids left alone
   on purpose: they key saved layouts, row caps and column choices in
   localStorage, so changing one silently resets every reader's arrangement.
   The same reason `network` still reads "RDMA ports" under an id that says
   otherwise. */
export const SECTIONS: SectionDef[] = [
  { id: 'history', label: 'System Activity' },
  { id: 'processes', label: 'GPU processes' },
  /* WAS 'Network', and drew two tables. The interfaces one moved into Network
     history when that grew a table of its own — six of its seven columns were
     already there. What is left is the RDMA table, which history cannot
     replace: per device:port where history collapses to one row per interface,
     and carrying the negotiated rate string, which is an info label rather
     than a series. The id is unchanged so saved layouts keep their place. */
  /* `network` -- the RDMA Ports card -- was retired 2026-08-29 (AE19): it is
     Network Activity's `ports` view now. A saved layout that still names it is
     read through the same `known` filter as any other unknown id and dropped. */
  /* A SEPARATE CARD, not more chips on System Activity. That panel already carries 15
     metrics and its chip row wraps to three lines at full width; six network
     chips would not join it, they would bury it.

     A section of its own also gives the fabric its own time range, which is the
     behaviour you want — correlating a 200Gb link saturating at 03:00 against
     GPU temperature means looking at two windows, not one — and it inherits
     drag-to-reorder, pairing, collapse, hide and the row cap for free.

     Saved layouts from before this shipped are already handled: `reconcile()`
     appends a section the saved order has never seen rather than dropping it,
     so it turns up in its default position instead of vanishing. */
  { id: 'network-history', label: 'Network Activity' },
  /* Live, not historical, which is why it sits with the other live cards
     rather than beside History. A GB10 exposes 18-23 thermal sensors and the
     dashboard showed two; this is the rest of them. */
  { id: 'thermal', label: 'Temperatures' },
  { id: 'models', label: 'Models' },
  { id: 'activity', label: 'Model activity' },
];

const DEFAULT_ORDER = SECTIONS.map((s) => s.id);

/* CARD INSTANCES (roadmap AF).
 *
 * A section id is either a KIND -- one of SECTIONS, the card as it has always
 * been -- or `kind#n`, a further copy of it. Two Network Activity cards can
 * then sit side by side, one on its ports view and one on its charts, which
 * is the one thing folding RDMA Ports into that card (AE19) gave up.
 *
 * The bare kind IS instance one, unchanged, so no saved layout migrates and
 * nothing a user has set moves. Everything the store keeps is keyed by id
 * already, so a copy gets its own position, size and visibility for free;
 * what had to change is that readers validate the KIND of an id rather than
 * the id itself, or a stored `kind#2` would be dropped on load as unknown --
 * which would silently delete the card. Every reader has a test with an
 * instance id in it for exactly that reason.
 */
export function kindOf(id: string): string {
  const i = id.indexOf('#');
  return i === -1 ? id : id.slice(0, i);
}

/** A copy, as opposed to the original: only copies can be removed. */
export function isCopy(id: string): boolean {
  return id.includes('#');
}

/** The card-local storage key for one instance. Instance one keeps the bare
 *  key it always had; a copy gets the key suffixed with its id, so two copies
 *  of a card do not fight over one localStorage entry. Components use this
 *  for the state that is theirs rather than the layout's -- a chosen view, a
 *  metric selection. */
export function instanceKey(key: string, id: string): string {
  return isCopy(id) ? `${key}:${id}` : key;
}

/** Drop a removed copy's card-local keys, which are suffixed `:kind#n` by
 *  `instanceKey`. Scanned rather than enumerated: the layout does not know
 *  which keys a component keeps, and should not have to. */
function purgeInstanceKeys(id: string) {
  try {
    const suffix = `:${id}`;
    for (const k of Object.keys(localStorage)) {
      if (k.startsWith('spark-dash.') && k.endsWith(suffix)) localStorage.removeItem(k);
    }
  } catch {
    // Nothing to purge, or no storage.
  }
}

/** Per section, because the sections are not alike.
 *
 * A card that draws TWO tables gets a lower cap, because the cap applies to
 * each of them — so 8 on such a card is up to 16 rows of section against 10 for
 * a card that draws one. That is `network-history` now; it used to be
 * `network`, until its interfaces table moved.
 */
const DEFAULT_ROWS: Record<string, number> = {
  processes: 10,
  models: 10,
  activity: 10,
  /* Lower for the same reason as `network`: this card draws TWO tables, one per
     division, and the cap applies to each — so 8 here is up to 16 rows of
     section against 10 for a card that draws one. */
  'network-history': 8,
  /* Lower again: this card draws one table PER DOMAIN — up to six — and the cap
     applies to each. 8 here is 21 rows of package sensors alone at three
     nodes. */
  thermal: 8,
};

function readColumns(available: string[] = DEFAULT_ORDER): Record<string, Zone> {
  try {
    const saved = JSON.parse(localStorage.getItem(COLUMN_KEY) ?? 'null');
    if (!saved || typeof saved !== 'object') return {};
    const known = new Set(available);
    const out: Record<string, Zone> = {};
    for (const [id, z] of Object.entries(saved)) {
      if (known.has(kindOf(id)) && (z === 'left' || z === 'right')) out[id] = z;
    }
    return out;
  } catch {
    return {};
  }
}


/** Plot height bounds, in px.
 *
 * The floor is where a line still reads as a shape rather than as a smear: at
 * 60px the axis labels alone eat most of the box. The ceiling is one plot
 * roughly filling a laptop viewport — past that a grid of eight charts stops
 * being small multiples, which is the entire reason they are small.
 */
export const MIN_PLOT_PX = 80;
export const MAX_PLOT_PX = 480;
/** uPlot's height when nobody has dragged one.
 *
 * MetricChart imports this for its own prop default rather than repeating the
 * number. Two literals that had to agree would be a silent divergence: charts
 * would render at one height and the grip would report another, and nothing
 * would error. */
export const DEFAULT_PLOT_PX = 132;

function readPlotHeights(available: string[] = DEFAULT_ORDER): Record<string, number> {
  try {
    const saved = JSON.parse(localStorage.getItem(PLOT_HEIGHT_KEY) ?? 'null');
    if (!saved || typeof saved !== 'object') return {};
    const known = new Set(available);
    const out: Record<string, number> = {};
    for (const [id, n] of Object.entries(saved)) {
      /* Clamped on the way in, not merely type-checked. A hand-edited or
         stale-from-an-older-range value is the same hazard `readRows` guards:
         a 4000px plot would push every other card off the page with no visible
         control to undo it. */
      if (known.has(kindOf(id)) && typeof n === 'number' && Number.isFinite(n)) {
        out[id] = clampPlot(n);
      }
    }
    return out;
  } catch {
    return {};
  }
}

/** Chart ROWS per page, for a card dragged below the plot floor.
 *
 * Tables obey "content that does not fit paginates" through their row cap.
 * Chart grids were the exception because a plot cannot shrink below readable --
 * MIN_PLOT_PX -- and so a card of eleven interface charts in three rows floored
 * at 584px, measured. This is the other half of the rule for charts: once the
 * plots are at the floor, further shrinking cuts rows per page instead, and the
 * rest is a page away. Absent means every row. */
export const MAX_PLOT_ROWS = 16;

function clampPlotRows(n: number): number {
  return Math.min(MAX_PLOT_ROWS, Math.max(1, Math.round(n)));
}

function readPlotRows(available: string[] = DEFAULT_ORDER): Record<string, number> {
  try {
    const saved = JSON.parse(localStorage.getItem(PLOT_ROWS_KEY) ?? 'null');
    if (!saved || typeof saved !== 'object') return {};
    const known = new Set(available);
    const out: Record<string, number> = {};
    for (const [id, n] of Object.entries(saved)) {
      if (known.has(kindOf(id)) && typeof n === 'number' && Number.isFinite(n)) out[id] = clampPlotRows(n);
    }
    return out;
  } catch {
    return {};
  }
}

function clampPlot(px: number): number {
  return Math.min(MAX_PLOT_PX, Math.max(MIN_PLOT_PX, Math.round(px)));
}

/** Row-cap bounds for a dragged value. `0` (uncapped) bypasses these — it is a
 *  sentinel, not a count. One row is a table that can still be read one row at
 *  a time; the ceiling is well past any table this dashboard draws, and exists
 *  only so a stored value cannot make a card taller than the page. */
export const MIN_ROWS = 1;
export const MAX_ROWS = 200;

/** A card's HELD height, in modules — what the reader dragged it to.
 *
 * Distinct from the height it needs. A card's span is normally `ceil(content)`,
 * so dragging it taller than its content did nothing: Models has eleven models,
 * and once the cap passed eleven the card simply stopped growing. That is the
 * right default and the wrong ceiling — with two independent columns, holding a
 * card taller than it needs is how you get their BOTTOMS to line up.
 *
 * IT IS ONLY EVER USER INPUT, and that is load-bearing rather than incidental.
 * `Section` derives a card's span by measuring the rendered card, so anything
 * that feeds back into that measurement grows without limit. A held value is a
 * constant: the card measures `max(natural, held)`, which reads back as exactly
 * `held` and stops. Never write to this from a measurement.
 */
function readCardSpans(available: string[] = DEFAULT_ORDER): Record<string, number> {
  try {
    const saved = JSON.parse(localStorage.getItem(CARD_SPAN_KEY) ?? 'null');
    if (!saved || typeof saved !== 'object') return {};
    const known = new Set(available);
    const out: Record<string, number> = {};
    for (const [id, n] of Object.entries(saved)) {
      if (known.has(kindOf(id)) && typeof n === 'number' && Number.isFinite(n)) out[id] = clampRows(n);
    }
    return out;
  } catch {
    return {};
  }
}

function clampRows(n: number): number {
  if (n === 0) return 0;
  return Math.min(MAX_ROWS, Math.max(MIN_ROWS, Math.round(n)));
}

function readRows(available: string[] = DEFAULT_ORDER): Record<string, number> {
  try {
    const saved = JSON.parse(localStorage.getItem(ROWS_KEY) ?? 'null');
    if (!saved || typeof saved !== 'object') return {};
    const known = new Set(available);
    const out: Record<string, number> = {};
    for (const [id, n] of Object.entries(saved)) {
      /* Any whole number in range, NOT just the offered choices.
       *
       * It used to be `ROW_CHOICES.includes(n)`, on the reasoning that a
       * hand-edited 100000 would defeat the point of the cap. The cap is still
       * defended — by the range — but the list is no longer the only source of
       * values: the corner grip drags the row count continuously, so 13 rows
       * is now a thing a reader can ask for. Validating against the list would
       * have thrown away every dragged value on the next reload, silently,
       * with the card simply back at its default. */
      if (known.has(kindOf(id)) && typeof n === 'number' && Number.isFinite(n)) {
        out[id] = clampRows(n);
      }
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
    ? saved.filter((id): id is string => typeof id === 'string' && known.has(kindOf(id)))
    : [];

  /* A kind with NO instance in the saved list is a section added in a later
     release, and is appended. A kind whose only instance is a copy counts as
     present: the copy is that card. */
  const present = new Set(fromSaved.map(kindOf));
  const appended = available.filter((kind) => !present.has(kind));
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
    return saved.filter((id): id is string => typeof id === 'string' && known.has(kindOf(id)));
  } catch {
    return [];
  }
}


export class Layout {
  order = $state<string[]>(read());
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

  /* PAGINATE OR SCROLL. Both answer "the content is taller than the card";
     they differ in what the drag means. Paging couples height to content
     through the row cap -- drag sets rows, the height follows. Scrolling
     decouples them: the held span IS the height, every row renders, and the
     panel scrolls. Global rather than per card, because it is a way of
     reading the page, not a property of one card. */
  overflow = $state<Overflow>(readOverflow());

  /* Which zone each section sits in. Absent means full width, so a section
     added in a later release spans the page rather than silently appearing in
     a column the reader may have scrolled past. */
  placement = $state<Record<string, Zone>>(readPlacement());

  /** Rows a section shows before it pages. Absent means the section's own
   *  default; `0` means uncapped. */
  rows = $state<Record<string, number>>(readRows());

  /** Plot height per chart-bearing section. Absent means DEFAULT_PLOT_PX.
   *
   * Height lives HERE rather than beside `rows` in settings because it is not
   * the same kind of choice: a row cap is a discrete pick from a list, and a
   * plot height is a continuous one you make by looking at the chart. So its
   * control is a grip on the card, and settings does not offer it a second
   * time — one thing, one control. */
  plotHeights = $state<Record<string, number>>(readPlotHeights());

  /** Chart rows per page, for chart cards shrunk past the plot floor. Absent
   *  means all rows. */
  plotRows = $state<Record<string, number>>(readPlotRows());

  /** Held card height per section, in modules. Absent means "as tall as the
   *  content needs", which is the default and the common case. */
  cardSpans = $state<Record<string, number>>(readCardSpans());

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
  drop = $state<DropTarget | null>(null);

  /** Reader-chosen order of the node/cluster cards, as keys. Empty means
   *  inventory order, which is `cluster.yml`'s. */
  nodeOrder = $state<string[]>(readNodeOrder());

  /** The group being dragged, and where a release would put it. Separate from
   *  the section drag state because the gestures differ: a node card has one
   *  axis and no half-width, so there is no zone, no band and no pairing. */
  nodeDragKey = $state<string | null>(null);
  nodeDrop = $state<{ anchorKey: string | null; before: boolean; y: number } | null>(null);

  /** Inventory order, with the reader's arrangement applied over it.
   *
   * Takes the keys AS THE PAGE FOUND THEM so the fallback is always the order
   * of cluster.yml — the same list that decides a node's colour, which is why
   * dragging cards repaints nothing.
   */
  orderGroups(keys: string[]): string[] {
    if (!this.nodeOrder.length) return keys;
    const known = new Set(keys);
    const seen = new Set<string>();
    const out: string[] = [];
    for (const k of this.nodeOrder) {
      if (known.has(k) && !seen.has(k)) {
        out.push(k);
        seen.add(k);
      }
    }
    // Anything the saved order never mentioned keeps its inventory position
    // relative to the rest, appended rather than dropped.
    for (const k of keys) if (!seen.has(k)) out.push(k);
    return out;
  }

  /** Move a group to sit before or after another. Anchored on a card for the
   *  same reason a section drag is: it is the card the affordance drew. */
  moveGroup(key: string, anchorKey: string | null, before: boolean, all: string[]) {
    const current = this.orderGroups(all);
    const next = current.filter((k) => k !== key);
    let at = next.length;
    if (anchorKey) {
      const i = next.indexOf(anchorKey);
      if (i !== -1) at = before ? i : i + 1;
    }
    next.splice(at, 0, key);
    this.nodeOrder = next;
    try {
      localStorage.setItem(NODE_ORDER_KEY, JSON.stringify(next));
    } catch {
      // Still applied for this session.
    }
  }

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
   *
   * `0` is that sentinel in storage, and NOTHING CAN PRODUCE ONE ANY MORE: it
   * came from a list in settings that the resize corner obviated, and a drag is
   * floored at MIN_ROWS on purpose so shrinking a card can never flip it to
   * "show everything". The translation stays because a layout saved before that
   * still round-trips through here.
   */
  rowsFor(id: string): number {
    // Scrolling shows everything; the cap is kept, not cleared, so switching
    // back finds it where it was.
    if (this.overflow === 'scroll') return Infinity;
    const n = this.rows[id] ?? DEFAULT_ROWS[kindOf(id)] ?? 10;
    return n === 0 ? Infinity : n;
  }

  /** The stored value, for settings to display. Distinct from `rowsFor`
   *  because 0 must read as "all" there, not as Infinity. */
  rowChoice(id: string): number {
    return this.rows[id] ?? DEFAULT_ROWS[kindOf(id)] ?? 10;
  }

  setRows(id: string, n: number) {
    this.rows = { ...this.rows, [id]: clampRows(n) };
    try {
      localStorage.setItem(ROWS_KEY, JSON.stringify(this.rows));
    } catch {
      // Still applied for this session.
    }
  }

  /** Height for one section's plots, in px. */
  plotHeight(id: string): number {
    return this.plotHeights[id] ?? DEFAULT_PLOT_PX;
  }

  setPlotHeight(id: string, px: number) {
    this.plotHeights = { ...this.plotHeights, [id]: clampPlot(px) };
    this.#savePlotHeights();
  }

  /** Back to this section's default cap — the counterpart of
   *  `resetPlotHeight`, and the same escape: a card dragged down to one row
   *  has a grip too small to aim at comfortably. */
  resetRows(id: string) {
    const { [id]: _drop, ...rest } = this.rows;
    this.rows = rest;
    try {
      localStorage.setItem(ROWS_KEY, JSON.stringify(this.rows));
    } catch {
      // Still applied for this session.
    }
  }

  /** Back to DEFAULT_PLOT_PX. The escape from a plot dragged to the floor,
   *  which is the one state a drag alone cannot comfortably undo — an 80px
   *  chart still has a grip, but finding it means knowing it is there. */
  resetPlotHeight(id: string) {
    const { [id]: _drop, ...rest } = this.plotHeights;
    this.plotHeights = rest;
    this.#savePlotHeights();
  }

  /** The held height, or 0 for "no floor — fit the content". */
  cardSpan(id: string): number {
    return this.cardSpans[id] ?? 0;
  }

  setCardSpan(id: string, n: number) {
    this.cardSpans = { ...this.cardSpans, [id]: clampRows(n) };
    this.#saveCardSpans();
  }

  clearCardSpan(id: string) {
    const { [id]: _drop, ...rest } = this.cardSpans;
    this.cardSpans = rest;
    this.#saveCardSpans();
  }

  #saveCardSpans() {
    try {
      localStorage.setItem(CARD_SPAN_KEY, JSON.stringify(this.cardSpans));
    } catch {
      // Still applied for this session.
    }
  }

  /** Chart rows per page, or Infinity for all. Infinity is what the CALLER
   *  must translate -- TableView's slice returns nothing for an Infinite page
   *  size, which its own comment warns about. */
  plotRowsFor(id: string): number {
    if (this.overflow === 'scroll') return Infinity;
    return this.plotRows[id] ?? Infinity;
  }

  setPlotRows(id: string, n: number) {
    this.plotRows = { ...this.plotRows, [id]: clampPlotRows(n) };
    this.#savePlotRows();
  }

  resetPlotRows(id: string) {
    const { [id]: _drop, ...rest } = this.plotRows;
    this.plotRows = rest;
    this.#savePlotRows();
  }

  setOverflow(mode: Overflow) {
    this.overflow = mode;
    try {
      localStorage.setItem(OVERFLOW_KEY, mode);
    } catch {
      // Still applied for this session.
    }
  }

  #savePlotRows() {
    try {
      localStorage.setItem(PLOT_ROWS_KEY, JSON.stringify(this.plotRows));
    } catch {
      // Still applied for this session.
    }
  }

  #savePlotHeights() {
    try {
      localStorage.setItem(PLOT_HEIGHT_KEY, JSON.stringify(this.plotHeights));
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

  /** The page, as the sequence of bands it renders as.
   *
   * Derived, never stored: `order` and `placement` already contain everything
   * this needs, so a layout saved before bands existed opens correctly.
   */
  get bands(): Band[] {
    const out: Band[] = [];
    let run: string[] = [];

    const flush = () => {
      if (!run.length) return;
      const left = run.filter((id) => this.zoneOf(id) === 'left');
      const right = run.filter((id) => this.zoneOf(id) === 'right');
      out.push({
        kind: 'cols',
        left,
        right,
        /* The anchor for a drop into this band's EMPTY side. Without it such a
           drop has no card to position against and would fall to the end of
           the page — the very bug bands exist to fix, reappearing in the one
           case that has no visible card to aim at. */
        last: run[run.length - 1],
      });
      run = [];
    };

    for (const id of this.visible) {
      if (this.zoneOf(id) === 'full') {
        flush();
        out.push({ kind: 'full', id });
      } else {
        run.push(id);
      }
    }
    flush();
    return out;
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
    this.setZone(id, this.zoneOf(id) === 'full' ? this.columnFor(id) : 'full');
  }

  /** WHICH COLUMN a full-width card would return to.
   *
   * Exported rather than inlined into `toggleWidth` because the resize corner
   * has to DRAW this before it happens: it shows the footprint a release would
   * produce, and a preview that worked it out separately could disagree with
   * the move it is previewing. One answer, two callers.
   */
  columnFor(id: string): Zone {
    return this.lastColumn[id] ?? this.#emptierColumn();
  }

  /** For a section that has never been in a column. */
  #emptierColumn(): Zone {
    return this.inZone('left').length <= this.inZone('right').length ? 'left' : 'right';
  }


  /** Place a dragged card against an ANCHOR card rather than at an index.
   *
   * `place()` below still serves the keyboard, where "up one within my column"
   * is genuinely an index. A drag is not: it aims at a specific card, and with
   * the page a sequence of bands the same index means different things in
   * different bands. Anchoring on the card under the pointer says exactly one
   * thing, and it is the thing the affordance drew.
   */
  placeAt(id: string, zone: Zone, anchorId: string | null, before: boolean) {
    if (zone !== 'full') this.#rememberColumn(id, zone);
    const next = this.order.filter((x) => x !== id);

    let at = next.length;
    if (anchorId) {
      const k = next.indexOf(anchorId);
      if (k !== -1) at = before ? k : k + 1;
    }

    next.splice(at, 0, id);
    this.order = next;
    this.placement = { ...this.placement, [id]: zone };
    this.#savePlacement();
    this.#save();
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

  /** Make two full-width cards into a left/right pair, in one move.
   *
   * The gesture this serves: drag a full-width card onto the outer third of
   * another full-width card. Going from the default single stack to two
   * columns previously meant aiming at a column that was empty, zero-height
   * and therefore invisible until a drag was already under way — you had to
   * know it was there. Aiming at a card you can see is the whole point.
   *
   * IT MOVES A CARD NOBODY DRAGGED, which is why it cannot be expressed as a
   * `place()`. The target becomes the other half. That is the behaviour asked
   * for and it is the only one that leaves the page consistent: sending the
   * dragged card to a column and leaving the target full width would put a
   * half-width card beside a full-width one with nothing across from it.
   *
   * The left card leads in `order` so the page-wide list reads the way the row
   * does. Note that adjacency in `order` does not by itself guarantee the two
   * render level with each other — the columns fill INDEPENDENTLY, which is
   * the property that stops a short section stranding space beside a tall one
   * — so a pair made while the columns already hold other cards lands adjacent
   * in order without necessarily lining up on screen.
   */
  pairWith(id: string, targetId: string, side: 'left' | 'right') {
    if (id === targetId) return;
    const other: Zone = side === 'left' ? 'right' : 'left';
    this.#rememberColumn(id, side);
    this.#rememberColumn(targetId, other);

    const next = this.order.filter((x) => x !== id);
    const at = next.indexOf(targetId);
    if (at === -1) return;
    next.splice(side === 'left' ? at : at + 1, 0, id);

    this.order = next;
    this.placement = { ...this.placement, [id]: side, [targetId]: other };
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
    /* setZone, NOT place: it changes the column and leaves `order` alone, so
       the card stays where it is in the sequence and simply becomes half
       width there. The old version appended to the end of the target zone,
       which under the previous structure sent the card to the bottom of the
       page — the keyboard's version of the bug bands fix, and it went
       unnoticed because the comment describing it called appending the only
       position that always exists. Under bands, staying put always exists. */
    this.setZone(id, ZONES[j]);
  }

  isHidden(id: string): boolean {
    return this.hidden.includes(id);
  }

  /** Take a section off the dashboard entirely, or put it back.
   *
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
    for (const id of this.order) if (isCopy(id)) purgeInstanceKeys(id);
    this.order = [...DEFAULT_ORDER];
    this.hidden = [];
    this.placement = {};
    this.lastColumn = {};
    this.rows = {};
    this.plotHeights = {};
    this.cardSpans = {};
    this.plotRows = {};
    this.nodeOrder = [];
    /* Switched-off columns go too. Same unrecoverability rule as hidden
       sections: anything that can remove a thing from the page must have one
       control that puts everything back, or a reader who forgets what they hid
       is stuck with a table they cannot explain. */
    columnStore.reset();
    this.setCompactCards(false);
    this.setOverflow('page');
    this.#savePlacement();
    try {
      localStorage.removeItem(COLUMN_KEY);
      localStorage.removeItem(ROWS_KEY);
      localStorage.removeItem(PLOT_HEIGHT_KEY);
      localStorage.removeItem(PLOT_ROWS_KEY);
      localStorage.removeItem(CARD_SPAN_KEY);
      localStorage.removeItem(NODE_ORDER_KEY);
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
  }

  /** True when nothing has been customised — order untouched, nothing
   *  nothing hidden. Drives whether "reset layout" is offered at
   *  all, so it has to account for all three or a customised dashboard has no
   *  way back. */
  get isDefault(): boolean {
    return (
      this.order.join(',') === DEFAULT_ORDER.join(',') &&
      this.hidden.length === 0 &&
      Object.keys(this.placement).length === 0 &&
      Object.keys(this.rows).length === 0 &&
      /* BOTH resize maps, not just `rows`. A chart card's drag writes only
         plotHeights and cardSpans, so with these missing the "reset layout"
         button never appeared for it and Settings' reset stayed disabled --
         the card could be dragged and then only un-dragged corner by corner.
         Found by a bug sweep, not by a user, which is the only good way. */
      Object.keys(this.plotHeights).length === 0 &&
      Object.keys(this.cardSpans).length === 0 &&
      Object.keys(this.plotRows).length === 0 &&
      this.nodeOrder.length === 0 &&
      !columnStore.customised &&
      !this.compactCards &&
      this.overflow === 'page'
    );
  }

  label(id: string): string {
    const base = SECTIONS.find((s) => s.id === kindOf(id))?.label ?? kindOf(id);
    // "Network Activity 2": the number is the instance, and the first has none.
    return isCopy(id) ? `${base} ${id.slice(id.indexOf('#') + 1)}` : base;
  }

  /** Another copy of a card, placed right after the one it was copied from,
   *  in the same zone, with the same size. The copy's own view state -- which
   *  metrics, which network view -- starts from the defaults: that is what
   *  the copy is FOR, and copying it would make two identical cards. */
  duplicate(id: string): string {
    const kind = kindOf(id);
    let n = 2;
    while (this.order.includes(`${kind}#${n}`)) n++;
    const copy = `${kind}#${n}`;
    const at = this.order.indexOf(id);
    const next = [...this.order];
    next.splice(at === -1 ? next.length : at + 1, 0, copy);
    this.order = next;
    this.placement = { ...this.placement, [copy]: this.zoneOf(id) };
    if (this.lastColumn[id]) this.lastColumn = { ...this.lastColumn, [copy]: this.lastColumn[id] };
    if (id in this.rows) this.rows = { ...this.rows, [copy]: this.rows[id] };
    if (id in this.plotHeights) this.plotHeights = { ...this.plotHeights, [copy]: this.plotHeights[id] };
    if (id in this.plotRows) this.plotRows = { ...this.plotRows, [copy]: this.plotRows[id] };
    if (id in this.cardSpans) this.cardSpans = { ...this.cardSpans, [copy]: this.cardSpans[id] };
    this.#saveAll();
    return copy;
  }

  /** Add a card of a kind from the page's own button.
   *
   * If the kind has a visible card, this is a COPY of its last instance --
   * placed right after it, so two Network Activity cards sit together, which
   * is the whole use case. If every instance of the kind is hidden, the
   * reader almost certainly wants the one they have back rather than a second
   * hidden one, so the last instance is shown instead. Returns the id that is
   * now on the page, for the caller to scroll to. */
  addCard(kind: string): string {
    const mine = this.order.filter((id) => kindOf(id) === kind);
    const visible = mine.filter((id) => !this.isHidden(id));
    if (visible.length) return this.duplicate(visible[visible.length - 1]);
    if (mine.length) {
      const id = mine[mine.length - 1];
      this.toggleHidden(id);
      return id;
    }
    // A kind with no instance at all cannot happen through the UI; reconcile
    // re-appends one on load. Make one anyway rather than fail.
    this.order = [...this.order, kind];
    this.#save();
    return kind;
  }

  /** How many cards of each kind the page has, hidden ones included -- what
   *  the add menu shows beside each name. */
  countOf(kind: string): number {
    return this.order.filter((id) => kindOf(id) === kind).length;
  }

  /** The card that was just added, for `Section` to lift for a moment so the
   *  reader sees where it landed on a page that may be long. Cleared by the
   *  same code that set it, after the moment. */
  landed = $state<string | null>(null);

  /** Remove a copy. The original cannot be removed, only hidden -- a kind
   *  with no instance would be re-appended on the next load anyway. */
  remove(id: string) {
    if (!isCopy(id)) return;
    const drop = <T>(m: Record<string, T>) => {
      const { [id]: _gone, ...rest } = m;
      return rest;
    };
    this.order = this.order.filter((x) => x !== id);
    this.hidden = this.hidden.filter((x) => x !== id);
    this.placement = drop(this.placement);
    this.lastColumn = drop(this.lastColumn);
    this.rows = drop(this.rows);
    this.plotHeights = drop(this.plotHeights);
    this.plotRows = drop(this.plotRows);
    this.cardSpans = drop(this.cardSpans);
    this.#saveAll();
    purgeInstanceKeys(id);
  }

  /** Every store this class keeps, written. Used where several change at
   *  once; the single-purpose savers are still what the setters use. */
  #saveAll() {
    this.#save();
    this.#savePlacement();
    try {
      localStorage.setItem(HIDDEN_KEY, JSON.stringify(this.hidden));
      localStorage.setItem(COLUMN_KEY, JSON.stringify(this.lastColumn));
      localStorage.setItem(ROWS_KEY, JSON.stringify(this.rows));
      localStorage.setItem(PLOT_HEIGHT_KEY, JSON.stringify(this.plotHeights));
      localStorage.setItem(PLOT_ROWS_KEY, JSON.stringify(this.plotRows));
      localStorage.setItem(CARD_SPAN_KEY, JSON.stringify(this.cardSpans));
    } catch {
      // Still applied for this session.
    }
  }
}
