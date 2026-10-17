/* Theme families and modes (roadmap BM), printed as JSON for
 * tests/test_themes_js.py. */

import { FAMILIES, MODES, migrate, paletteOf } from '../../frontend/src/lib/themes.ts';

const legacy = ['auto', 'dark', 'light', 'cyberpunk', 'forest', 'slate', 'paper', 'contrast', 'nvidia', 'gone', null];
console.log(JSON.stringify({
  families: FAMILIES,
  modes: MODES.map((m) => m.id),
  migrated: Object.fromEntries(legacy.map((k) => [String(k), migrate(k)])),
  palettes: FAMILIES.map((f) => [paletteOf(f.id, true), paletteOf(f.id, false)]),
  unknown: [paletteOf('gone', true), paletteOf('gone', false)],
}));
