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

const STORAGE_KEY = 'spark-dash.theme.v1';

/** A theme that actually exists as a block of CSS custom properties. */
export type PaletteId =
  | 'dark'
  | 'light'
  | 'cyberpunk'
  | 'forest'
  | 'contrast'
  | 'slate'
  | 'paper';

/** What the reader can choose. `auto` is a rule, not a palette. */
export type ThemeId = PaletteId | 'auto';

export interface ThemeDef {
  id: ThemeId;
  label: string;
  /** Whether charts should draw on a dark surface. Absent for `auto`, whose
   *  answer depends on the system and can change while the page is open. */
  dark?: boolean;
}

export const THEMES: ThemeDef[] = [
  // First, and the default: a reader whose machine is in light mode should not
  // have to find this menu to stop being handed a dark dashboard.
  { id: 'auto', label: 'Auto' },
  { id: 'dark', label: 'Dark', dark: true },
  { id: 'light', label: 'Light', dark: false },
  { id: 'cyberpunk', label: 'Cyberpunk', dark: true },
  { id: 'forest', label: 'Forest', dark: true },
  { id: 'slate', label: 'Slate', dark: true },
  { id: 'paper', label: 'Paper', dark: false },
  // Last, and deliberately: it is the only entry here chosen for a reason
  // other than taste, so it reads as the end of the list rather than as one
  // more colour scheme among the others.
  { id: 'contrast', label: 'High contrast', dark: true },
];

const DARK_PALETTES = new Set<PaletteId>([
  'dark',
  'cyberpunk',
  'forest',
  'slate',
  'contrast',
]);
const IDS = new Set<string>(THEMES.map((t) => t.id));

const DARK_QUERY = '(prefers-color-scheme: dark)';

function systemPrefersDark(): boolean {
  // Defaults to dark when the browser cannot answer, which preserves the
  // behaviour every existing reader already has.
  return globalThis.matchMedia?.(DARK_QUERY).matches ?? true;
}

/** Themes that have been renamed, so a stored choice survives the rename.
 *
 *  Without this a renamed theme silently reverts the reader to the default,
 *  which reads as "my setting was forgotten" rather than "that theme is called
 *  something else now". `forest` shipped as `nvidia` for a few hours on
 *  2026-08-19 and was renamed because the palette is a restrained green rather
 *  than the bold brand colours the name promised. */
const RENAMED: Record<string, ThemeId> = { nvidia: 'forest' };

function read(): ThemeId {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved && RENAMED[saved]) return RENAMED[saved];
    if (saved && IDS.has(saved)) return saved as ThemeId;
  } catch {
    // Storage unavailable (private mode, quota). Fall through to the default.
  }
  return 'dark';
}

function resolve(id: ThemeId): PaletteId {
  return id === 'auto' ? (systemPrefersDark() ? 'dark' : 'light') : id;
}

export class Theme {
  /** What the reader chose. May be `auto`, which is not a palette. */
  current = $state<ThemeId>(read());

  /** The palette actually in force. This is what charts must key off: `auto`
   *  is never applied to the document, and it changes underneath while the
   *  page is open. */
  resolved = $state<PaletteId>('dark');

  #media: MediaQueryList | null = null;

  constructor() {
    this.resolved = resolve(this.current);
    // Applied synchronously in the constructor, not from an $effect. Charts
    // resolve CSS custom properties into literal canvas colours when they
    // build, and effect ordering isn't guaranteed — a chart that built before
    // the attribute landed would paint with the previous theme's values.
    this.#apply(this.resolved);

    // The system preference can change while the page is open — at sunset, on
    // most machines. That is a theme change nobody clicked, and it is exactly
    // the case that would otherwise leave every chart painted in the previous
    // palette, because canvases cannot re-read CSS variables.
    this.#media = globalThis.matchMedia?.(DARK_QUERY) ?? null;
    this.#media?.addEventListener('change', this.#onSystemChange);
  }

  set(id: ThemeId) {
    this.current = id;
    this.resolved = resolve(id);
    this.#apply(this.resolved);
    try {
      localStorage.setItem(STORAGE_KEY, id);
    } catch {
      // Still applied for this session.
    }
  }

  /** Whether the active palette sits on a dark surface — charts need to know,
   *  since they can't read CSS variables from a canvas. */
  get isDark(): boolean {
    return DARK_PALETTES.has(this.resolved);
  }

  destroy() {
    this.#media?.removeEventListener('change', this.#onSystemChange);
  }

  #onSystemChange = () => {
    if (this.current !== 'auto') return;
    const next = resolve('auto');
    if (next === this.resolved) return;
    this.resolved = next;
    this.#apply(next);
  };

  #apply(id: PaletteId) {
    // Always a real palette id — `auto` has no CSS block, and writing it here
    // would leave the document on :root's defaults with no way to reach light.
    document.documentElement.dataset.theme = id;
  }
}
