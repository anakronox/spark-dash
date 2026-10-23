/* Chart grid geometry and time labels, actually executed.
 *
 * Run from tests/test_chart_grid.py with TZ=UTC, so local time is fixed and the
 * label cases below mean the same thing on every machine.
 */

import assert from 'node:assert/strict';

import {
  MIN_CHART_PX,
  crossesDay,
  fitRange,
  mirrorRange,
  decadeSplits,
  niceStep,
  gaugeCols,
  gridCols,
  MIN_GAUGE_PX,
  maxColsFor,
  onLastRow,
  stampOf,
  timeLabels,
} from '../../frontend/src/lib/chart-grid.ts';

let passed = 0;
let failed = 0;
function test(name, fn) {
  try {
    fn();
    passed++;
  } catch (err) {
    failed++;
    console.error(`FAIL ${name}\n${err.message}\n`);
  }
}

const at = (iso) => Date.parse(iso) / 1000;

test('the maximum snaps 1 / 2 / 4', () => {
  assert.deepEqual([1, 2, 3, 4, 7, 11].map(maxColsFor), [1, 2, 2, 4, 4, 4]);
});

test('a full-width card on a wide screen keeps four', () => {
  assert.equal(gridCols(4, 1640, 14), 4);
});

test('a half-width card halves to two, not four 190px charts', () => {
  assert.equal(gridCols(4, 820, 14), 2);
});

test('a phone-width card is one column', () => {
  assert.equal(gridCols(4, 380, 14), 1);
});

test('three charts never go four across', () => {
  assert.equal(gridCols(3, 3000, 14), 2);
});

test('an unmeasured width keeps the maximum rather than flashing one column', () => {
  assert.equal(gridCols(8, 0, 14), 4);
});

test('the halving threshold is MIN_CHART_PX per chart', () => {
  const exact = 4 * MIN_CHART_PX + 3 * 14;
  assert.equal(gridCols(4, exact, 14), 4);
  assert.equal(gridCols(4, exact - 1, 14), 2);
});

test('only the bottom row is the last row, including a short one', () => {
  // 11 charts, 4 across: rows of 4, 4, 3.
  const last = Array.from({ length: 11 }, (_, i) => onLastRow(i, 11, 4));
  assert.deepEqual(last, [false, false, false, false, false, false, false, false, true, true, true]);
  assert.deepEqual([0, 1].map((i) => onLastRow(i, 2, 4)), [true, true]);
});

test('within one day the labels are times, one line each', () => {
  const s = [at('2026-10-12T20:45:00Z'), at('2026-10-12T21:00:00Z'), at('2026-10-12T21:15:00Z')];
  assert.deepEqual(timeLabels(s), ['8:45pm', '9:00pm', '9:15pm']);
});

test('across days, midnight is the date and the rest are hours', () => {
  const s = [at('2026-10-11T18:00:00Z'), at('2026-10-12T00:00:00Z'), at('2026-10-12T06:00:00Z'), at('2026-10-12T12:30:00Z')];
  assert.deepEqual(timeLabels(s), ['6pm', 'Oct 12', '6am', '12:30pm']);
});

test('a window from 10pm names its midnight even when every tick is on the next day', () => {
  const s = [at('2026-10-12T00:00:00Z'), at('2026-10-12T06:00:00Z'), at('2026-10-12T12:00:00Z')];
  assert.deepEqual(timeLabels(s, at('2026-10-11T22:00:00Z'), at('2026-10-12T22:00:00Z')), ['Oct 12', '6am', '12pm']);
});

test('a week of midnights is a row of dates', () => {
  const s = [0, 1, 2].map((d) => at(`2026-10-0${6 + d}T00:00:00Z`));
  assert.deepEqual(timeLabels(s), ['Oct 6', 'Oct 7', 'Oct 8']);
});

test('no splits, no labels', () => {
  assert.deepEqual(timeLabels([]), []);
});

test('the tooltip names the day only when the window crosses one', () => {
  const t = at('2026-10-12T09:05:00Z');
  assert.equal(stampOf(t, false), '9:05am');
  assert.equal(stampOf(t, true), 'Oct 12 9:05am');
  assert.equal(crossesDay(at('2026-10-12T00:10:00Z'), at('2026-10-12T23:50:00Z')), false);
  assert.equal(crossesDay(at('2026-10-11T23:50:00Z'), at('2026-10-12T00:10:00Z')), true);
});

test('steps are 1, 2 or 5 times a power of ten', () => {
  assert.deepEqual([0.7, 3.75, 12, 30, 450].map(niceStep), [1, 5, 20, 50, 500]);
});

test('a real climb fills the chart: 45-70°C on a 0-100 axis', () => {
  // 40-80 in steps of 10: the data fills 62% of the plot, against 25% of a
  // fixed 0-100 axis.
  const [lo, hi] = fitRange(45, 70, { ceil: 100, minSpan: 15 });
  assert.ok(lo >= 40 && lo <= 45, `lo ${lo}`);
  assert.ok(hi >= 70 && hi <= 80, `hi ${hi}`);
  assert.ok((70 - 45) / (hi - lo) >= 0.6, `fills ${(70 - 45) / (hi - lo)}`);
});

test('a flat line stays flat: memory at 93% gets the minimum span, pinned under the ceiling', () => {
  const [lo, hi] = fitRange(92.8, 93.3, { ceil: 100, minSpan: 20 });
  assert.equal(hi, 100);
  assert.ok(hi - lo >= 20, `span ${hi - lo}`);
  assert.ok(lo <= 92.8);
});

test('never below the floor: an idle GPU at 0W', () => {
  const [lo, hi] = fitRange(0, 3, { ceil: 300, minSpan: 50 });
  assert.equal(lo, 0);
  assert.ok(hi >= 50 && hi <= 60, `hi ${hi}`);
});

test('a reading past the ceiling widens the range rather than being clipped', () => {
  const [, hi] = fitRange(80, 112, { ceil: 104, minSpan: 15 });
  assert.ok(hi >= 112, `hi ${hi}`);
});

test('gridlines land on whole steps', () => {
  const [lo, hi] = fitRange(51.3, 66.8, { ceil: 100, minSpan: 15 });
  assert.equal(lo % 5, 0);
  assert.equal(hi % 5, 0);
});

test('no data falls back to the full range', () => {
  assert.deepEqual(fitRange(null, null, { ceil: 300, minSpan: 50 }), [0, 300]);
  assert.deepEqual(fitRange(null, null, { minSpan: 50 }), [0, 50]);
});

test("a mirrored range reaches each side's own peak", () => {
  assert.deepEqual(mirrorRange(-500, 1000).map((v) => Math.round(v)), [-550, 1100]);
});

test('a quiet direction keeps a fifth of the height', () => {
  const [lo, hi] = mirrorRange(-1, 1000);
  assert.equal(Math.round(lo), -220);
  assert.equal(Math.round(hi), 1100);
});

test('no traffic at all is a small symmetric range, not a collapsed one', () => {
  assert.deepEqual(mirrorRange(0, 0), [-1, 1]);
  assert.deepEqual(mirrorRange(null, null), [-1, 1]);
});

test('decade gridlines on both sides of zero, from 1 kb/s', () => {
  assert.deepEqual(decadeSplits(-25000, 3e6), [-10000, -1000, 0, 1000, 10000, 100000, 1000000]);
  assert.deepEqual(decadeSplits(-500, 800), [0]);
});

test('gauges fill the row with no 1 / 2 / 4 snap: three panels, one row', () => {
  assert.equal(gaugeCols(3, 1900, 14), 3);
  assert.equal(gaugeCols(4, 1900, 16), 4);
  assert.equal(gaugeCols(3, 3000, 14), 3);
});

test('gauges drop a column only below MIN_GAUGE_PX each', () => {
  const exact = 3 * MIN_GAUGE_PX + 2 * 14;
  assert.equal(gaugeCols(3, exact, 14), 3);
  assert.equal(gaugeCols(3, exact - 1, 14), 2);
  assert.equal(gaugeCols(3, 200, 14), 1);
});

test('gauges before the first measure keep every panel on one row', () => {
  assert.equal(gaugeCols(5, 0, 14), 5);
  assert.equal(gaugeCols(0, 0, 14), 1);
});

console.log(`${passed}/${passed + failed} passed`);
if (failed) process.exit(1);
