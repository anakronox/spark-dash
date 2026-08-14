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

export type ThemeId = 'dark' | 'light' | 'cyberpunk';

export interface ThemeDef {
  id: ThemeId;
  label: string;
  /** Whether charts should draw on a dark surface. */
  dark: boolean;
}

export const THEMES: ThemeDef[] = [
  { id: 'dark', label: 'Dark', dark: true },
  { id: 'light', label: 'Light', dark: false },
  { id: 'cyberpunk', label: 'Cyberpunk', dark: true },
];

const IDS = new Set<string>(THEMES.map((t) => t.id));

function read(): ThemeId {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved && IDS.has(saved)) return saved as ThemeId;
  } catch {
    // Storage unavailable (private mode, quota). Fall through to the default.
  }
  return 'dark';
}

export class Theme {
  current = $state<ThemeId>(read());

  constructor() {
    // Applied synchronously in the constructor, not from an $effect. Charts
    // resolve CSS custom properties into literal canvas colours when they
    // build, and effect ordering isn't guaranteed — a chart that built before
    // the attribute landed would paint with the previous theme's values.
    this.#apply(this.current);
  }

  set(id: ThemeId) {
    this.current = id;
    this.#apply(id);
    try {
      localStorage.setItem(STORAGE_KEY, id);
    } catch {
      // Still applied for this session.
    }
  }

  /** Whether the active theme sits on a dark surface — charts need to know,
   *  since they can't read CSS variables from a canvas. */
  get isDark(): boolean {
    return THEMES.find((t) => t.id === this.current)?.dark ?? true;
  }

  #apply(id: ThemeId) {
    document.documentElement.dataset.theme = id;
  }
}
