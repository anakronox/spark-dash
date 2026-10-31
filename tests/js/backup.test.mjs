/* Layout backup and restore (roadmap CQ), actually executed.
 *
 * Run from tests/test_backup_js.py, which transpiles this with the esbuild
 * Vite already brings and runs it under node.
 */

import assert from 'node:assert/strict';

import { collect, fileName, parse, restore } from '../../frontend/src/lib/backup.ts';

let passed = 0;
let failed = 0;
const test = (name, fn) => { try { fn(); passed++; } catch (e) { failed++; console.error(`FAIL ${name}\n${e.message}\n`); } };

/** localStorage's shape over a Map. */
function store(entries) {
  const m = new Map(Object.entries(entries));
  return {
    get length() { return m.size; },
    key: (i) => [...m.keys()][i] ?? null,
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
    dump: () => Object.fromEntries([...m.entries()].sort()),
  };
}

const NOW = new Date('2026-10-31T21:00:00Z');

test('a backup is every layout key, including copied cards, and nothing else', () => {
  const s = store({
    'spark-dash.theme.v1': 'solarized',
    'spark-dash.card-order.v1': '{"trends.metrics":["gpu_power"]}',
    'spark-dash.trend-view.v1:system-activity#2': 'gauges',
    'spark-dash.tour-done.v1': '1',
    'spark-dash.network-ack.v1': '[]',
    'other-app.thing': 'x',
  });
  const b = collect(s, NOW);
  assert.equal(b.app, 'spark-dash-layout');
  assert.equal(b.created, '2026-10-31T21:00:00.000Z');
  assert.deepEqual(Object.keys(b.items), [
    'spark-dash.card-order.v1',
    'spark-dash.theme.v1',
    'spark-dash.trend-view.v1:system-activity#2',
  ]);
});

test('the file is named for the day it was saved', () => {
  assert.equal(fileName(NOW), 'spark-dash-layout-2026-10-31.json');
});

test('a backup survives its own round trip', () => {
  const b = collect(store({ 'spark-dash.theme.v1': 'darcula' }), NOW);
  const p = parse(JSON.stringify(b));
  assert.equal(p.ok, true);
  assert.deepEqual(p.backup.items, { 'spark-dash.theme.v1': 'darcula' });
});

test('anything that is not a layout backup is refused, with a reason', () => {
  const bad = (text) => { const p = parse(text); assert.equal(p.ok, false); return p.error; };
  assert.match(bad('not json'), /not JSON/);
  assert.match(bad('{"items":{}}'), /Not a spark-dash layout backup/);
  assert.match(bad('{"app":"spark-dash-layout","version":2,"items":{}}'), /different version/);
  assert.match(bad('{"app":"spark-dash-layout","version":1,"items":[]}'), /no layout/);
  assert.match(bad('{"app":"spark-dash-layout","version":1,"items":{"evil.key":"x"}}'), /Unexpected entry/);
  assert.match(bad('{"app":"spark-dash-layout","version":1,"items":{"spark-dash.tour-done.v1":"1"}}'), /Unexpected entry/);
  assert.match(bad('{"app":"spark-dash-layout","version":1,"items":{"spark-dash.theme.v1":3}}'), /not text/);
  assert.match(bad('x'.repeat(1_000_001)), /Too large/);
});

test('a restore replaces the layout, clears what the backup lacks, and keeps the rest', () => {
  const s = store({
    'spark-dash.theme.v1': 'cyberpunk',
    'spark-dash.column-widths.v1': '{"models":{"model":300}}',
    'spark-dash.tour-done.v1': '1',
    'other-app.thing': 'x',
  });
  restore(s, { app: 'spark-dash-layout', version: 1, created: '', items: { 'spark-dash.theme.v1': 'solarized', 'spark-dash.card-order.v1': '{}' } });
  assert.deepEqual(s.dump(), {
    'other-app.thing': 'x',
    'spark-dash.card-order.v1': '{}',
    'spark-dash.theme.v1': 'solarized',
    'spark-dash.tour-done.v1': '1',
  });
});

console.log(`${passed}/${passed + failed} passed`);
if (failed) process.exit(1);
