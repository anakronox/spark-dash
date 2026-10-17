/** Theme families and modes (roadmap BM), kept out of the store so the tests
 *  can run them under node. */

/** A block of CSS custom properties in app.css. Every family has one for
 *  each mode; the older ids keep their names so their blocks and the
 *  checker's records of them stay put. */
export type PaletteId =
  | 'dark'
  | 'light'
  | 'catppuccin'
  | 'catppuccin-light'
  | 'solarized'
  | 'solarized-light'
  | 'darcula'
  | 'darcula-light'
  | 'cyberpunk'
  | 'cyberpunk-light'
  | 'forest'
  | 'forest-light'
  | 'slate'
  | 'slate-light'
  | 'paper-dark'
  | 'paper'
  | 'contrast'
  | 'contrast-light';

export type FamilyId =
  | 'default'
  | 'catppuccin'
  | 'solarized'
  | 'darcula'
  | 'cyberpunk'
  | 'forest'
  | 'slate'
  | 'paper'
  | 'contrast';

export type Mode = 'auto' | 'dark' | 'light';

export interface FamilyDef {
  id: FamilyId;
  label: string;
  dark: PaletteId;
  light: PaletteId;
}

export const FAMILIES: FamilyDef[] = [
  { id: 'default', label: 'Default', dark: 'dark', light: 'light' },
  { id: 'catppuccin', label: 'Catppuccin', dark: 'catppuccin', light: 'catppuccin-light' },
  { id: 'solarized', label: 'Solarized', dark: 'solarized', light: 'solarized-light' },
  { id: 'darcula', label: 'Darcula', dark: 'darcula', light: 'darcula-light' },
  { id: 'cyberpunk', label: 'Cyberpunk', dark: 'cyberpunk', light: 'cyberpunk-light' },
  { id: 'forest', label: 'Forest', dark: 'forest', light: 'forest-light' },
  { id: 'slate', label: 'Slate', dark: 'slate', light: 'slate-light' },
  { id: 'paper', label: 'Paper', dark: 'paper-dark', light: 'paper' },
  // Last, and deliberately: it is the only entry here chosen for a reason
  // other than taste, so it reads as the end of the list rather than as one
  // more colour scheme among the others.
  { id: 'contrast', label: 'High contrast', dark: 'contrast', light: 'contrast-light' },
];

const FAMILY_IDS = () => new Set<string>(FAMILIES.map((f) => f.id));

export const MODES: { id: Mode; label: string }[] = [
  { id: 'auto', label: 'Auto' },
  { id: 'dark', label: 'Dark' },
  { id: 'light', label: 'Light' },
];

/** A theme id stored before BM, as the family and mode it now is. `forest`
 *  shipped as `nvidia` for a few hours on 2026-08-19. */
export function migrate(saved: string | null): { family: FamilyId; mode: Mode } {
  if (saved === 'auto') return { family: 'default', mode: 'auto' };
  if (saved === 'light') return { family: 'default', mode: 'light' };
  if (saved === 'paper') return { family: 'paper', mode: 'light' };
  if (saved === 'nvidia') return { family: 'forest', mode: 'dark' };
  if (saved && FAMILY_IDS().has(saved)) return { family: saved as FamilyId, mode: 'dark' };
  return { family: 'default', mode: 'dark' };
}

export function paletteOf(family: FamilyId, dark: boolean): PaletteId {
  const f = FAMILIES.find((x) => x.id === family) ?? FAMILIES[0];
  return dark ? f.dark : f.light;
}

