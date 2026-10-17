/** Layout presets (roadmap BK): whole arrangements to start from, offered by
 *  the setup guide and in Settings -> Appearance.
 *
 * A preset is the `spark-dash.*` keys a reader would have saved by arranging
 * the page that way by hand. That means each store reads it the way it reads
 * its own saved state, so a preset needs no code path of its own, and
 * anything changed after choosing one is saved over it as usual.
 *
 * Applying one clears the layout first, like Reset layout, so nothing from
 * the previous arrangement survives into the next, and then reloads, because
 * every store read its key once at start. */
import { SEEN_KEY, TOUR_KEY } from './onboarding';

export const PRESET_KEY = 'spark-dash.layout-preset.v1';
/** The layout as it stood right after a preset was applied, written by the
 *  Layout on its first load afterwards. "Reset layout" is offered while the
 *  page differs from it. */
export const BASELINE_KEY = 'spark-dash.layout-baseline.v1';

export interface Preset {
  id: string;
  name: string;
  about: string;
  /** localStorage values, by key. */
  values: Record<string, string>;
  /** The picker's thumbnail: blocks on a 12 x 8 grid, [x, y, w, h, kind]. */
  sketch: [number, number, number, number, SketchKind][];
}

export type SketchKind = 'nodes' | 'chart' | 'gauges' | 'table';

const json = (v: unknown) => JSON.stringify(v);

export const PRESETS: Preset[] = [
  {
    id: 'side-by-side',
    name: 'Side by side',
    about:
      'Compact nodes with Clients beside them, temperatures as gauges, then the two activity charts and the two tables in pairs. Most on one screen.',
    sketch: [[0, 0, 7, 1.6, 'nodes'], [7.2, 0, 4.8, 1.6, 'table'], [0, 1.8, 12, 1.4, 'gauges'], [0, 3.4, 5.9, 2.6, 'chart'], [6.1, 3.4, 5.9, 2.6, 'chart'], [0, 6.2, 5.9, 1.8, 'table'], [6.1, 6.2, 5.9, 1.8, 'table']],
    values: {
      'spark-dash.compact-cards.v1': '1',
      'spark-dash.section-order.v1': json([
        'clients', 'thermal', 'history', 'network-history', 'processes', 'models', 'activity',
      ]),
      'spark-dash.section-placement.v1': json({
        clients: 'nodes',
        thermal: 'full',
        history: 'left',
        'network-history': 'right',
        processes: 'left',
        models: 'right',
      }),
      'spark-dash.section-hidden.v1': json(['activity']),
      'spark-dash.trend-metrics.v1': json([
        'gpu_utilization', 'gpu_temperature', 'memory_used_percent', 'gpu_power', 'gpu_clock',
        'cpu_utilization', 'tokens_per_second', 'prompt_tokens_per_second', 'psi_some_avg10',
        'swap_io', 'disk_busy', 'temperature_headroom',
      ]),
      'spark-dash.thermal-view.v1': json('gauges'),
      'spark-dash.thermal-hidden.v1': json(['wireless']),
      'spark-dash.thermal-combine.v1': json({
        table: {},
        gauges: { package: 'average', gpu: 'average', storage: 'average' },
      }),
      'spark-dash.network-mode.v1': 'charts',
      'spark-dash.clients-range.v1': '6h',
    },
  },
  {
    id: 'stacked',
    name: 'Stacked',
    about:
      'Full node elements, then every element at full width in one column, with every sensor listed. The most detail, and the most scrolling.',
    sketch: [[0, 0, 12, 2.2, 'nodes'], [0, 2.4, 12, 1.8, 'chart'], [0, 4.4, 12, 1.1, 'table'], [0, 5.7, 12, 1.1, 'chart'], [0, 7, 12, 1, 'table']],
    values: {},
  },
  {
    id: 'wall',
    name: 'Wall display',
    about:
      'Gauges only, for a screen across the room: compact nodes, temperatures, system and network activity as dials, and the models. No tables to read.',
    sketch: [[0, 0, 12, 1.2, 'nodes'], [0, 1.4, 12, 1.8, 'gauges'], [0, 3.4, 5.9, 2.8, 'gauges'], [6.1, 3.4, 5.9, 2.8, 'gauges'], [0, 6.4, 12, 1.6, 'table']],
    values: {
      'spark-dash.compact-cards.v1': '1',
      'spark-dash.section-order.v1': json([
        'thermal', 'history', 'network-history', 'models', 'processes', 'activity', 'clients',
      ]),
      'spark-dash.section-placement.v1': json({
        thermal: 'full',
        history: 'left',
        'network-history': 'right',
        models: 'full',
      }),
      'spark-dash.section-hidden.v1': json(['processes', 'activity', 'clients']),
      'spark-dash.thermal-view.v1': json('gauges'),
      'spark-dash.thermal-hidden.v1': json(['wireless']),
      'spark-dash.trend-view.v1': 'gauges',
      'spark-dash.network-mode.v1': 'gauges',
    },
  },
];

export const DEFAULT_PRESET = PRESETS[0].id;

/** Kept across a preset or a reset: the theme and the page's name, what the
 *  reader has already been shown, and Settings' own pane and width. */
const KEEP = new Set([
  'spark-dash.theme.v1',
  'spark-dash.theme-mode.v1',
  'spark-dash.page-title.v1',
  SEEN_KEY,
  TOUR_KEY,
  'spark-dash.settings-pane.v1',
  'spark-dash.settings-width.v1',
  'spark-dash.report-pdf-orientation.v1',
]);

export function presetById(id: string | null): Preset {
  return PRESETS.find((p) => p.id === id) ?? PRESETS[0];
}

/** The preset last applied, or null on a page arranged before presets. */
export function currentPreset(storage: Storage = localStorage): string | null {
  try {
    const id = storage.getItem(PRESET_KEY);
    return PRESETS.some((p) => p.id === id) ? id : null;
  } catch {
    return null;
  }
}

/** Clear the layout and write the preset's keys. The caller reloads. */
export function applyPreset(id: string, storage: Storage = localStorage) {
  const preset = presetById(id);
  try {
    for (const k of Object.keys(storage)) {
      if (k.startsWith('spark-dash.') && !KEEP.has(k)) storage.removeItem(k);
    }
    for (const [k, v] of Object.entries(preset.values)) storage.setItem(k, v);
    storage.setItem(PRESET_KEY, preset.id);
  } catch {
    // No storage: the page stays as it is.
  }
}

/** A first visit starts on the default preset. Only a browser with nothing of
 *  this page's layout saved and the guide never shown counts as one, so a
 *  reader who has been using the page keeps what they had. Run before the app
 *  mounts, so no reload is needed. */
export function seedFirstVisit(storage: Storage = localStorage): boolean {
  try {
    if (storage.getItem(PRESET_KEY) !== null || storage.getItem(SEEN_KEY) !== null) return false;
    const KEPT_ANYWAY = new Set(['spark-dash.theme.v1', 'spark-dash.theme-mode.v1', 'spark-dash.page-title.v1']);
    if (Object.keys(storage).some((k) => k.startsWith('spark-dash.') && !KEPT_ANYWAY.has(k))) return false;
    applyPreset(DEFAULT_PRESET, storage);
    return true;
  } catch {
    return false;
  }
}
