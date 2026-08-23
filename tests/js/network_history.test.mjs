/* The Network history grouping, actually executed.
 *
 * Every other frontend guard in this repo is source-level — a regex over the
 * file, because Svelte runes need a compiler and there is no JS test runner
 * here. That is the right trade for a drag gesture, whose real behaviour lives
 * in the DOM anyway.
 *
 * It is the wrong trade for this. `network-history.ts` is plain TypeScript with
 * no runes and no DOM, and it is nearly all branching: which interfaces are
 * quiet, what order they come in, whether a fault chart exists at all. A regex
 * asserting that the word "quiet" appears would pass on code that gets every
 * one of those backwards.
 *
 * Run from tests/test_network_history.py, which transpiles this with the
 * esbuild that Vite already brings and runs it under node. Skipped when neither
 * is installed, so a checkout without node_modules still gets a green suite.
 */

import assert from 'node:assert/strict';

import {
  DROPS,
  ERRORS,
  RX,
  TX,
  buildGrid,
  chartsFor,
  columnName,
  columnNode,
  linkKey,
  links,
} from '../../frontend/src/lib/network-history.ts';

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

/** A HistorySeries as the backend returns it. */
const series = (node, iface) => ({
  node,
  labels: { node, interface: iface },
  points: [],
});

const name = (node, iface, metric) => columnName({ metric, series: series(node, iface) });

/** Build the (names, columns) pair the card hands to buildGrid. */
function dataset(spec) {
  const names = [];
  const columns = [];
  for (const [key, values] of Object.entries(spec)) {
    const [node, iface, metric] = key.split('/');
    names.push(name(node, iface, metric));
    columns.push(values);
  }
  return { names, columns };
}

// ---------------------------------------------------------------- packing

test('a packed name gives its node back', () => {
  assert.equal(columnNode(name('sparky', 'enP7s7', RX)), 'sparky');
});

test('the separator cannot collide with a device name', () => {
  // Real names carry dots, dashes, colons and slashes. If any of those were the
  // separator, this link would unpack as a different node.
  const odd = linkKey('gx10-1.local', 'br-1a2b/vlan3:0');
  assert.equal(columnNode(`${odd}${odd.slice(-1)}`.slice(0, odd.length)), 'gx10-1.local');
  assert.equal(columnNode(name('gx10-1.local', 'br-1a2b/vlan3:0', RX)), 'gx10-1.local');
});

// ---------------------------------------------------------------- ordering

test('links come back in node order, then by interface name', () => {
  const { names } = dataset({
    [`sparkjr/wlP9s9/${RX}`]: [],
    [`sparky/enP7s7/${RX}`]: [],
    [`sparkjr/enP7s7/${RX}`]: [],
    [`sparketa/enp1s0f0np0/${RX}`]: [],
  });
  const order = links(names, ['sparky', 'sparketa', 'sparkjr']).map((l) => `${l.node} ${l.iface}`);
  assert.deepEqual(order, [
    'sparky enP7s7',
    'sparketa enp1s0f0np0',
    'sparkjr enP7s7',
    'sparkjr wlP9s9',
  ]);
});

test('a node history knows but the inventory does not is kept, not dropped', () => {
  // A node removed from cluster.yml still has samples for the rest of the
  // window, and IS drawn — so it has to be in the grid.
  const { names } = dataset({ [`retired/eth0/${RX}`]: [], [`sparky/enP7s7/${RX}`]: [] });
  const order = links(names, ['sparky']).map((l) => l.node);
  assert.deepEqual(order, ['sparky', 'retired']);
});

// ------------------------------------------------------------- throughput

test('a throughput chart draws receive solid and transmit dashed', () => {
  const { names, columns } = dataset({
    [`sparky/enP7s7/${RX}`]: [1, 2],
    [`sparky/enP7s7/${TX}`]: [3, 4],
  });
  const byName = new Map(names.map((n, i) => [n, columns[i]]));
  const [chart] = chartsFor(links(names, ['sparky'])[0], byName);

  assert.deepEqual(chart.names, ['rx', 'tx']);
  assert.equal(chart.metric.dashed, 'tx', 'transmit must be the dashed line');
  assert.equal(chart.metric.label, 'enP7s7');
  assert.equal(chart.metric.si, true, 'bits need an SI axis or 580223842 is unreadable');
  assert.equal(chart.metric.unit, 'b/s');
  assert.equal(chart.metric.scaleMax, undefined, 'link speed is not a shared ceiling');
});

test('an interface reporting one direction still charts', () => {
  const { names, columns } = dataset({ [`sparky/enP7s7/${RX}`]: [1, 2] });
  const byName = new Map(names.map((n, i) => [n, columns[i]]));
  const [chart] = chartsFor(links(names, ['sparky'])[0], byName);
  assert.deepEqual(chart.names, ['rx']);
});

// ------------------------------------------------------------------ faults

test('no fault chart on a healthy link', () => {
  const { names, columns } = dataset({
    [`sparky/enP7s7/${RX}`]: [10],
    [`sparky/enP7s7/${ERRORS}`]: [0],
    [`sparky/enP7s7/${DROPS}`]: [0],
  });
  const byName = new Map(names.map((n, i) => [n, columns[i]]));
  const built = chartsFor(links(names, ['sparky'])[0], byName);
  assert.equal(built.length, 1, 'a flat zero is a chart-sized hole, not a reassurance');
});

test('a fault chart appears on the first non-zero sample', () => {
  const { names, columns } = dataset({
    [`sparky/enP7s7/${RX}`]: [10, 10],
    [`sparky/enP7s7/${ERRORS}`]: [0, 3],
    [`sparky/enP7s7/${DROPS}`]: [0, 0],
  });
  const byName = new Map(names.map((n, i) => [n, columns[i]]));
  const built = chartsFor(links(names, ['sparky'])[0], byName);
  assert.equal(built.length, 2);
  assert.equal(built[1].metric.label, 'enP7s7 faults');
  assert.deepEqual(built[1].names, ['errors', 'drops']);
  assert.equal(built[1].metric.dashed, 'drops');
  assert.equal(built[1].metric.si, undefined, 'a fault count is not a bit rate');
});

test('drops alone are enough to raise the chart', () => {
  // Errors and drops are kept apart because they mean different things — a drop
  // is backpressure, an error is usually physical — so either one must be able
  // to surface the chart on its own.
  const { names, columns } = dataset({
    [`sparky/enP7s7/${RX}`]: [10],
    [`sparky/enP7s7/${ERRORS}`]: [0],
    [`sparky/enP7s7/${DROPS}`]: [2],
  });
  const byName = new Map(names.map((n, i) => [n, columns[i]]));
  assert.equal(chartsFor(links(names, ['sparky'])[0], byName).length, 2);
});

// -------------------------------------------------------------------- grid

const busyAndIdle = () =>
  dataset({
    [`sparky/enP7s7/${RX}`]: [10, 20],
    [`sparky/enP7s7/${TX}`]: [1, 2],
    [`sparky/wlP9s9/${RX}`]: [0, 0],
    [`sparky/wlP9s9/${TX}`]: [0, 0],
  });

test('an interface with no traffic at all is hidden, and counted', () => {
  const { names, columns } = busyAndIdle();
  const grid = buildGrid(names, columns, ['sparky'], null, false);
  assert.deepEqual(grid.charts.map((c) => c.metric.label), ['enP7s7']);
  assert.equal(grid.quiet, 1, 'the count is what tells the reader something is hidden');
});

test('the idle toggle brings it back without changing the count', () => {
  const { names, columns } = busyAndIdle();
  const grid = buildGrid(names, columns, ['sparky'], null, true);
  assert.deepEqual(grid.charts.map((c) => c.metric.label), ['enP7s7', 'wlP9s9']);
  assert.equal(grid.quiet, 1);
});

test('an idle 200Gb link is NOT quiet — it still carries a trickle', () => {
  // The measured case this filter has to get right: sparketa's RoCE port peaks
  // at 288 b/s over 24h. Filtering on `monitored` or on a threshold would drop
  // the fabric off the card; filtering on "any traffic at all" keeps it, with
  // its own axis, which is the entire argument for small multiples.
  const { names, columns } = dataset({
    [`sparketa/enP2p1s0f0np0/${RX}`]: [0, 288, 0],
    [`sparketa/enP2p1s0f0np0/${TX}`]: [0, 0, 0],
  });
  const grid = buildGrid(names, columns, ['sparketa'], null, false);
  assert.equal(grid.charts.length, 1);
  assert.equal(grid.quiet, 0);
});

test('a fault chart sits immediately after the interface it belongs to', () => {
  const { names, columns } = dataset({
    [`sparky/enP7s7/${RX}`]: [10],
    [`sparky/enP7s7/${ERRORS}`]: [4],
    [`sparky/wlP9s9/${RX}`]: [5],
  });
  const grid = buildGrid(names, columns, ['sparky'], null, false);
  assert.deepEqual(grid.charts.map((c) => c.metric.label), [
    'enP7s7',
    'enP7s7 faults',
    'wlP9s9',
  ]);
});

test('soloing a node drops the other nodes links', () => {
  const { names, columns } = dataset({
    [`sparky/enP7s7/${RX}`]: [10],
    [`sparketa/enP7s7/${RX}`]: [10],
  });
  const grid = buildGrid(names, columns, ['sparky', 'sparketa'], ['sparketa'], false);
  assert.deepEqual(grid.charts.map((c) => c.link.node), ['sparketa']);
});

test('a null sample is not traffic', () => {
  // A node down for the window returns nulls, not zeroes. Treating null as
  // traffic would keep a dead node on the grid as a chart with no line.
  const { names, columns } = dataset({ [`sparky/enP7s7/${RX}`]: [null, null] });
  assert.equal(buildGrid(names, columns, ['sparky'], null, false).charts.length, 0);
});

test('every chart key is unique — the grid is keyed on it', () => {
  const { names, columns } = dataset({
    [`sparky/enP7s7/${RX}`]: [10],
    [`sparky/enP7s7/${ERRORS}`]: [1],
    [`sparketa/enP7s7/${RX}`]: [10],
  });
  const keys = buildGrid(names, columns, ['sparky', 'sparketa'], null, true).charts.map(
    (c) => c.key,
  );
  assert.equal(new Set(keys).size, keys.length, 'a duplicate key makes Svelte reuse the wrong chart');
});

let failed = 0;
for (const [label, fn] of tests) {
  try {
    fn();
  } catch (err) {
    failed++;
    console.error(`FAIL  ${label}\n      ${err.message.split('\n').join('\n      ')}`);
  }
}
console.log(`${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
