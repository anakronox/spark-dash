/* Column order (roadmap CO), actually executed.
 *
 * Run from tests/test_column_order_js.py, which transpiles this with the
 * esbuild Vite already brings and runs it under node.
 */

import assert from 'node:assert/strict';

import { applyOrder, dropBefore, isNoOp, moveBefore } from '../../frontend/src/lib/column-order.ts';

let passed = 0;
let failed = 0;
const test = (name, fn) => { try { fn(); passed++; } catch (e) { failed++; console.error(`FAIL ${name}\n${e.message}\n`); } };

const COLS = [
  { key: 'model', required: true },
  { key: 'state' },
  { key: 'node' },
  { key: 'tok' },
  { key: 'kv' },
];
const keys = (cols) => cols.map((c) => c.key);

test('no stored order is the default order', () => {
  assert.deepEqual(keys(applyOrder(COLS, [])), ['model', 'state', 'node', 'tok', 'kv']);
});

test('a stored order applies after the pinned columns', () => {
  assert.deepEqual(keys(applyOrder(COLS, ['kv', 'tok', 'node', 'state'])), ['model', 'kv', 'tok', 'node', 'state']);
});

test('a required column in the stored order stays pinned first', () => {
  assert.deepEqual(keys(applyOrder(COLS, ['tok', 'model', 'state', 'node', 'kv'])), ['model', 'tok', 'state', 'node', 'kv']);
});

test('a stored key the table no longer has is dropped, and duplicates once', () => {
  assert.deepEqual(keys(applyOrder(COLS, ['gone', 'kv', 'kv', 'state', 'node', 'tok'])), ['model', 'kv', 'state', 'node', 'tok']);
});

test('a column added since the drag lands after its default neighbour', () => {
  // `node` was not there when the reader dragged: it follows `state`.
  assert.deepEqual(keys(applyOrder(COLS, ['kv', 'state', 'tok'])), ['model', 'kv', 'state', 'node', 'tok']);
  // A new first movable column has no predecessor and goes first.
  assert.deepEqual(keys(applyOrder(COLS, ['kv', 'node', 'tok'])), ['model', 'state', 'kv', 'node', 'tok']);
});

test('moveBefore moves in front, to the end, and leaves bad targets alone', () => {
  const o = ['state', 'node', 'tok', 'kv'];
  assert.deepEqual(moveBefore(o, 'kv', 'state'), ['kv', 'state', 'node', 'tok']);
  assert.deepEqual(moveBefore(o, 'state', null), ['node', 'tok', 'kv', 'state']);
  assert.deepEqual(moveBefore(o, 'state', 'tok'), ['node', 'state', 'tok', 'kv']);
  assert.deepEqual(moveBefore(o, 'node', 'node'), o);
  assert.deepEqual(moveBefore(o, 'node', 'gone'), o);
});

test('a drop lands in front of the first header whose middle is right of it', () => {
  const edges = [
    { key: 'state', left: 100, right: 200 },
    { key: 'node', left: 200, right: 260 },
    { key: 'tok', left: 260, right: 300 },
  ];
  assert.equal(dropBefore(edges, 90), 'state');
  assert.equal(dropBefore(edges, 149), 'state');
  assert.equal(dropBefore(edges, 151), 'node');
  assert.equal(dropBefore(edges, 281), null);
});

test('a drop that would leave the column where it is, is not a move', () => {
  const shown = ['state', 'node', 'tok'];
  assert.equal(isNoOp(shown, 'node', 'node'), true);
  assert.equal(isNoOp(shown, 'node', 'tok'), true);
  assert.equal(isNoOp(shown, 'tok', null), true);
  assert.equal(isNoOp(shown, 'node', 'state'), false);
  assert.equal(isNoOp(shown, 'state', null), false);
});

console.log(`${passed}/${passed + failed} passed`);
if (failed) process.exit(1);
