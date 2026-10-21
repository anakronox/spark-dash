/** Theme selection, persisted per browser.
 *
 * Every theme is stepped separately rather than derived by inverting another
 * one. An automatic inversion produces colours that happen to be readable
 * rather than colours chosen to be, and it's how a status palette ends up
 * illegible on the surface it actually sits on.
 *
 * Each theme's node colours are validated for colourblind separation, chroma
 * and contrast against that theme's own surface. Two candidate themes were cut
 * for failing: a green-phosphor look, where green/teal/amber sat below the
 * separation floor even for full colour vision, and a muted slate palette that
 * read as grey.
 */

import { CARD_HEADERS, FAMILIES, migrate, paletteOf, type CardHeaders, type FamilyId, type Mode, type PaletteId } from './themes';
export { CARD_HEADERS, FAMILIES, MODES, migrate, paletteOf } from './themes';
export type { CardHeaders, FamilyDef, FamilyId, Mode, PaletteId } from './themes';

/** The family last chosen. Before BM this key held one theme id, light or
 *  dark built in; `read()` migrates those. */
const STORAGE_KEY = 'spark-dash.theme.v1';
/** Auto, dark or light (BM). Absent on a browser from before BM. */
export const MODE_KEY = 'spark-dash.theme-mode.v1';
/** The card header band (BQ). Absent means the default, tinted. */
const HEADERS_KEY = 'spark-dash.card-headers.v1';

const FAMILY = new Map(FAMILIES.map((f) => [f.id, f]));

const DARK_QUERY = '(prefers-color-scheme: dark)';

function systemPrefersDark(): boolean {
  // Defaults to dark when the browser cannot answer, which preserves the
  // behaviour every existing reader already has.
  return globalThis.matchMedia?.(DARK_QUERY).matches ?? true;
}

function read(): { family: FamilyId; mode: Mode } {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    const mode = localStorage.getItem(MODE_KEY);
    if (mode === null) return migrate(saved);
    return {
      family: FAMILY.has(saved as FamilyId) ? (saved as FamilyId) : 'default',
      mode: mode === 'light' || mode === 'auto' ? mode : 'dark',
    };
  } catch {
    // Storage unavailable (private mode, quota). Fall through to the default.
    return { family: 'default', mode: 'dark' };
  }
}

function readHeaders(): CardHeaders {
  try {
    const saved = localStorage.getItem(HEADERS_KEY);
    return CARD_HEADERS.some((h) => h.id === saved) ? (saved as CardHeaders) : 'tinted';
  } catch {
    return 'tinted';
  }
}

export class Theme {
  family = $state<FamilyId>('default');
  /** What the reader chose. `auto` follows the system. */
  mode = $state<Mode>('dark');

  /** The palette actually in force. This is what charts must key off: it
   *  changes underneath while the page is open when the mode is auto. */
  resolved = $state<PaletteId>('dark');
  /** Whether that palette is a dark one. */
  isDark = $state(true);
  /** The band behind each card's title. */
  headers = $state<CardHeaders>('tinted');

  #media: MediaQueryList | null = null;

  constructor() {
    const { family, mode } = read();
    this.family = family;
    this.mode = mode;
    this.headers = readHeaders();
    document.documentElement.dataset.cardHeaders = this.headers;
    // Applied synchronously in the constructor, not from an $effect. Charts
    // resolve CSS custom properties into literal canvas colours when they
    // build, and effect ordering isn't guaranteed — a chart that built before
    // the attribute landed would paint with the previous theme's values.
    this.#refresh();

    // The system preference can change while the page is open — at sunset, on
    // most machines. That is a theme change nobody clicked, and it is exactly
    // the case that would otherwise leave every chart painted in the previous
    // palette, because canvases cannot re-read CSS variables.
    this.#media = globalThis.matchMedia?.(DARK_QUERY) ?? null;
    this.#media?.addEventListener('change', this.#onSystemChange);
  }

  setFamily(id: FamilyId) {
    this.family = id;
    this.#refresh();
    this.#save();
  }

  setMode(mode: Mode) {
    this.mode = mode;
    this.#refresh();
    this.#save();
  }

  setHeaders(headers: CardHeaders) {
    this.headers = headers;
    document.documentElement.dataset.cardHeaders = headers;
    try {
      localStorage.setItem(HEADERS_KEY, headers);
    } catch {
      // Still applied for this session.
    }
  }

  /** The header's switch: the other of dark and light from what is showing,
   *  which takes the page off auto. */
  toggle() {
    this.setMode(this.isDark ? 'light' : 'dark');
  }

  destroy() {
    this.#media?.removeEventListener('change', this.#onSystemChange);
  }

  #onSystemChange = () => {
    if (this.mode === 'auto') this.#refresh();
  };

  #refresh() {
    const dark = this.mode === 'auto' ? systemPrefersDark() : this.mode === 'dark';
    const next = paletteOf(this.family, dark);
    this.isDark = dark;
    if (next === this.resolved && document.documentElement.dataset.theme === next) return;
    this.resolved = next;
    document.documentElement.dataset.theme = next;
  }

  #save() {
    try {
      localStorage.setItem(STORAGE_KEY, this.family);
      localStorage.setItem(MODE_KEY, this.mode);
    } catch {
      // Still applied for this session.
    }
  }
}
