/* Layout presets (roadmap BK), run against an in-memory localStorage and
 * printed as JSON for tests/test_presets_js.py. */

import { PRESETS, PRESET_KEY, DEFAULT_PRESET, applyPreset, currentPreset, seedFirstVisit } from '../../frontend/src/lib/presets.ts';

class Mem {
  constructor(init = {}) {
    Object.assign(this, init);
  }
  getItem(k) {
    return Object.prototype.hasOwnProperty.call(this, k) ? this[k] : null;
  }
  setItem(k, v) {
    this[k] = String(v);
  }
  removeItem(k) {
    delete this[k];
  }
}
const dump = (m) => ({ ...m });

const fresh = new Mem();
const freshThemed = new Mem({ 'spark-dash.theme.v1': 'paper' });
const arranged = new Mem({ 'spark-dash.section-order.v1': '["models"]' });
const seen = new Mem({ 'spark-dash.onboarding-seen.v1': '1' });

const switched = new Mem({
  'spark-dash.theme.v1': 'cyberpunk',
  'spark-dash.onboarding-seen.v1': '1',
  'spark-dash.tour-done.v1': '1',
  'spark-dash.layout-baseline.v1': '[]',
  'spark-dash.section-order.v1': '["models"]',
  'spark-dash.trend-view.v1:history#2': 'gauges',
  'other-site.key': 'x',
});
applyPreset('wall', switched);

console.log(JSON.stringify({
  presets: PRESETS,
  default: DEFAULT_PRESET,
  presetKey: PRESET_KEY,
  seeded: [seedFirstVisit(fresh), seedFirstVisit(freshThemed), seedFirstVisit(arranged), seedFirstVisit(seen)],
  fresh: dump(fresh),
  freshThemed: dump(freshThemed),
  arranged: dump(arranged),
  switched: dump(switched),
  current: [currentPreset(switched), currentPreset(new Mem()), currentPreset(new Mem({ [PRESET_KEY]: 'gone' }))],
}));
