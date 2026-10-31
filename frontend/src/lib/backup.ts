/** Back up and restore this browser's layout (roadmap CQ).
 *
 * Everything that shapes the page lives in localStorage under `spark-dash.`:
 * theme, arrangement, each card's view, size, columns and order, and every
 * copied card's own keys (suffixed `:kind#n`). A backup is all of them in one
 * JSON file; a restore replaces them and reloads, since each store reads its
 * key once at start.
 *
 * NOTHING SECRET IS HERE. The MCP and assistant keys are files beside the
 * cluster file on the server; this is view preference only, so a backup can be
 * handed to someone else's browser.
 *
 * Plain TypeScript with no runes and no DOM, so tests/js runs it for real.
 */

export const PREFIX = 'spark-dash.';
const APP = 'spark-dash-layout';
const VERSION = 1;

/** What this browser has done or seen, not how the page is set up. Left out
 *  of a backup, and left alone by a restore: a restored page should not
 *  replay the tour, nor forget which network events were acknowledged. */
export const NOT_LAYOUT = new Set([
  'spark-dash.onboarding-seen.v1',
  'spark-dash.tour-done.v1',
  'spark-dash.network-ack.v1',
  'spark-dash.settings-pane.v1',
  'spark-dash.mcp-client.v1',
]);

/** A layout is a few kilobytes; this is far above any real one and below
 *  what would make a stray file hang the page. */
const MAX_BYTES = 1_000_000;

export interface Backup {
  app: string;
  version: number;
  created: string;
  items: Record<string, string>;
}

/** The subset of Storage these functions use, so tests can pass a Map. */
export interface KeyStore {
  readonly length: number;
  key(index: number): string | null;
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

function isLayoutKey(key: string): boolean {
  return key.startsWith(PREFIX) && !NOT_LAYOUT.has(key.split(':')[0]);
}

function layoutKeys(store: KeyStore): string[] {
  const out: string[] = [];
  for (let i = 0; i < store.length; i++) {
    const key = store.key(i);
    if (key && isLayoutKey(key)) out.push(key);
  }
  return out.sort();
}

export function collect(store: KeyStore, now = new Date()): Backup {
  const items: Record<string, string> = {};
  for (const key of layoutKeys(store)) {
    const value = store.getItem(key);
    if (value !== null) items[key] = value;
  }
  return { app: APP, version: VERSION, created: now.toISOString(), items };
}

/** `spark-dash-layout-2026-10-31.json` */
export function fileName(now = new Date()): string {
  return `spark-dash-layout-${now.toISOString().slice(0, 10)}.json`;
}

export type Parsed = { ok: true; backup: Backup } | { ok: false; error: string };

/** A file's text as a backup, or why it is not one. Keys outside the
 *  dashboard's own, and values that are not strings, are refused rather than
 *  skipped: a file that is partly wrong is not one to half-apply. */
export function parse(text: string): Parsed {
  if (text.length > MAX_BYTES) return { ok: false, error: 'Too large to be a layout backup.' };
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    return { ok: false, error: 'Not a layout backup: the file is not JSON.' };
  }
  if (!data || typeof data !== 'object' || (data as Backup).app !== APP) {
    return { ok: false, error: 'Not a spark-dash layout backup.' };
  }
  const b = data as Backup;
  if (b.version !== VERSION) {
    return { ok: false, error: `Saved by a different version of the dashboard (format ${b.version}).` };
  }
  if (!b.items || typeof b.items !== 'object' || Array.isArray(b.items)) {
    return { ok: false, error: 'The backup has no layout in it.' };
  }
  for (const [key, value] of Object.entries(b.items)) {
    if (!isLayoutKey(key)) return { ok: false, error: `Unexpected entry "${key}".` };
    if (typeof value !== 'string') return { ok: false, error: `Entry "${key}" is not text.` };
  }
  return { ok: true, backup: { app: APP, version: VERSION, created: String(b.created ?? ''), items: b.items } };
}

/** Replace this browser's layout with the backup's. Layout keys the backup
 *  does not have are REMOVED, so a preference left at its default when the
 *  backup was taken is at its default after the restore, not kept from now. */
export function restore(store: KeyStore, backup: Backup): void {
  for (const key of layoutKeys(store)) {
    if (!(key in backup.items)) store.removeItem(key);
  }
  for (const [key, value] of Object.entries(backup.items)) store.setItem(key, value);
}
