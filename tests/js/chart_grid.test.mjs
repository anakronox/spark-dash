/* Chart grid geometry and time labels, actually executed.
 *
 * Run from tests/test_chart_grid.py with TZ=UTC, so local time is fixed and the
 * label cases below mean the same thing on every machine.
 */

import assert from 'node:assert/strict';

import {
  MIN_CHART_PX,
  crossesDay,
  gridCols,
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

console.log(`${passed}/${passed + failed} passed`);
if (failed) process.exit(1);
