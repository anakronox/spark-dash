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
  BURST_RATIO,
  LINK_UP,
  TIER_BURST,
  TIER_STATE,
  TIER_STEADY,
  buildGrid,
  buildRows,
  byImportance,
  chartsFor,
  columnName,
  columnNode,
  linkKey,
  links,
  portChart,
  portIsNotable,
  pairPorts,
  ports,
} from '../../frontend/src/lib/network-history.ts';
import { sparkPath } from '../../frontend/src/lib/sparkline.ts';

const tests = [];
/** Every chart on the card, divisions flattened — for the assertions that are
 *  about ordering or gating rather than about the division itself. */
const flat = (grid) => grid.groups.flatMap((g) => g.charts);
const divisionOf = (grid, label) =>
  (grid.groups.find((g) => g.charts.some((c) => c.metric.label === label)) ?? {}).key;

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
  // `enP7s7` is what `ip link` says and what the Network table prints. The
  // caption is uppercased as a style, which is right for a phrase and wrong for
  // a name — a caption nobody can paste into a shell is not the name of the
  // thing any more.
  assert.equal(chart.metric.verbatim, true, 'a device name must keep its case');
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
  // A count over the window, not a rate — so no unit. "3/s" would be a claim
  // about a second; "3" is a claim about what happened.
  assert.equal(built[1].metric.unit, '');
  assert.equal(built[1].metric.si, undefined, 'a fault count is not a bit rate');
  assert.equal(built[1].metric.verbatim, true, 'the fault caption names a device too');
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
  assert.deepEqual(flat(grid).map((c) => c.metric.label), ['enP7s7']);
  assert.equal(grid.quiet, 1, 'the count is what tells the reader something is hidden');
});

test('the idle toggle brings it back without changing the count', () => {
  const { names, columns } = busyAndIdle();
  const grid = buildGrid(names, columns, ['sparky'], null, true);
  assert.deepEqual(flat(grid).map((c) => c.metric.label), ['enP7s7', 'wlP9s9']);
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
  assert.equal(flat(grid).length, 1);
  assert.equal(grid.quiet, 0);
});

test('a fault chart sits immediately after the interface it belongs to', () => {
  const { names, columns } = dataset({
    [`sparky/enP7s7/${RX}`]: [10],
    [`sparky/enP7s7/${ERRORS}`]: [4],
    [`sparky/wlP9s9/${RX}`]: [5],
  });
  const grid = buildGrid(names, columns, ['sparky'], null, false);
  assert.deepEqual(flat(grid).map((c) => c.metric.label), [
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
  assert.deepEqual(flat(grid).map((c) => c.link.node), ['sparketa']);
});

test('a null sample is not traffic', () => {
  // A node down for the window returns nulls, not zeroes. Treating null as
  // traffic would keep a dead node on the grid as a chart with no line.
  const { names, columns } = dataset({ [`sparky/enP7s7/${RX}`]: [null, null] });
  assert.equal(flat(buildGrid(names, columns, ['sparky'], null, false)).length, 0);
});

test('every chart key is unique — the grid is keyed on it', () => {
  const { names, columns } = dataset({
    [`sparky/enP7s7/${RX}`]: [10],
    [`sparky/enP7s7/${ERRORS}`]: [1],
    [`sparketa/enP7s7/${RX}`]: [10],
  });
  const keys = flat(buildGrid(names, columns, ['sparky', 'sparketa'], null, true)).map(
    (c) => c.key,
  );
  assert.equal(new Set(keys).size, keys.length, 'a duplicate key makes Svelte reuse the wrong chart');
});

// -------------------------------------------------------------- rdma ports

const portRow = (node, device, iface, column, port = '1') => ({
  labels: { node, device, port, ...(iface ? { interface: iface } : {}) },
  column,
});

test('two label variants of one port merge into one history', () => {
  // Measured: a `cluster` label was added to the targets part way through the
  // window, so 12 of 18 keys have two series, each null where the other has
  // samples. Keeping one would truncate the chart at the relabel — which looks
  // exactly like the port having stopped reporting.
  const merged = ports([
    portRow('sparky', 'roceP2p1s0f1', '', [1, 1, null, null]),
    portRow('sparky', 'roceP2p1s0f1', '', [null, null, 0, 1]),
  ]);
  assert.equal(merged.length, 1, 'one physical port must be one entry');
  assert.deepEqual(merged[0].column, [1, 1, 0, 1]);
});

test('the variant that knows the pairing wins', () => {
  // The agent upgrade that ships AC1c lands mid-window: one variant carries
  // `interface` and the other predates it.
  const merged = ports([
    portRow('sparky', 'roceP2p1s0f1', '', [1, null]),
    portRow('sparky', 'roceP2p1s0f1', 'enP2p1s0f1np1', [null, 0]),
  ]);
  assert.equal(merged[0].iface, 'enP2p1s0f1np1');
});

test('a port that was up all window is not drawn', () => {
  // A flat line at "up" restates the green dot already on the Network table.
  assert.equal(portIsNotable({ column: [1, 1, 1] }), false);
});

test('a port that was down all window IS drawn', () => {
  // Measured: two of sparky's ports have read 0 for seven days straight. The
  // live table shows that as a red dot with no hint it has been a week.
  assert.equal(portIsNotable({ column: [0, 0, 0] }), true);
});

test('a flap is drawn', () => {
  assert.equal(portIsNotable({ column: [1, 0, 1] }), true);
});

test('nulls alone are not a state change', () => {
  assert.equal(portIsNotable({ column: [1, null, 1] }), false);
});

test('a port chart is a stepped two-state plot, named by its device', () => {
  const chart = portChart({
    node: 'sparky', device: 'roceP2p1s0f1', port: '1',
    iface: 'enP2p1s0f1np1', column: [1, 0],
  });
  assert.deepEqual(chart.metric.states, ['down', 'up']);
  // `roceP2p1s0f1` is what ibstat prints. Captioning it with the netdev name
  // would make it look like a second chart about the throughput above it.
  assert.equal(chart.metric.label, 'roceP2p1s0f1 port 1');
  assert.equal(chart.metric.verbatim, true);
  assert.equal(chart.metric.si, undefined, 'a state is not a rate');
});

test('a port chart lands under the interface it shares a cable with', () => {
  const { names, columns } = dataset({
    [`sparky/enP7s7/${RX}`]: [10],
    [`sparky/enP2p1s0f1np1/${RX}`]: [5],
  });
  const rdma = ports([portRow('sparky', 'roceP2p1s0f1', 'enP2p1s0f1np1', [1, 0])]);
  const grid = buildGrid(names, columns, ['sparky'], null, false, rdma);
  assert.deepEqual(flat(grid).map((c) => c.metric.label), [
    'enP2p1s0f1np1',
    'roceP2p1s0f1 port 1',
    'enP7s7',
  ]);
});

test('a port whose pairing is unknown still gets charted, at the end of Fabric', () => {
  // An agent from before AC1c and no live snapshot either. Dropping the chart
  // would hide a flapping port on exactly the deployment least equipped to
  // notice. It cannot be placed under a link, so it goes last in its division.
  const { names, columns } = dataset({
    [`sparky/enP2p1s0f0np0/${RX}`]: [10],
    [`sparky/enP7s7/${RX}`]: [10],
  });
  const rdma = ports([portRow('sparky', 'roceP2p1s0f1', '', [1, 0])]);
  const grid = buildGrid(names, columns, ['sparky'], null, false, rdma, FABRIC);
  const fab = grid.groups.find((g) => g.key === 'fabric');
  assert.deepEqual(fab.charts.map((c) => c.metric.label), [
    'enP2p1s0f0np0',
    'roceP2p1s0f1 port 1',
  ]);
});

test('an idle link whose RoCE port flapped is NOT hidden', () => {
  // A silent link whose port dropped is not an idle link — it is the most
  // interesting thing on the card.
  const { names, columns } = dataset({
    [`sparky/enP2p1s0f1np1/${RX}`]: [0, 0],
    [`sparky/enP2p1s0f1np1/${TX}`]: [0, 0],
  });
  const rdma = ports([portRow('sparky', 'roceP2p1s0f1', 'enP2p1s0f1np1', [1, 0])]);
  const grid = buildGrid(names, columns, ['sparky'], null, false, rdma);
  assert.deepEqual(flat(grid).map((c) => c.metric.label), [
    'enP2p1s0f1np1',
    'roceP2p1s0f1 port 1',
  ]);
  assert.equal(grid.quiet, 1, 'it is still counted as idle — it just is not hidden');
});

test('soloing a node drops the other nodes ports too', () => {
  const { names, columns } = dataset({
    [`sparky/enP7s7/${RX}`]: [10],
    [`sparketa/enP7s7/${RX}`]: [10],
  });
  const rdma = ports([
    portRow('sparky', 'roceP2p1s0f1', '', [0]),
    portRow('sparketa', 'roceP2p1s0f1', '', [0]),
  ]);
  const grid = buildGrid(names, columns, ['sparky', 'sparketa'], ['sparketa'], false, rdma);
  assert.deepEqual(flat(grid).map((c) => c.link.node), ['sparketa', 'sparketa']);
});

test('one node port does not attach to another node same-named interface', () => {
  // `enP7s7` exists on all three boxes. Keying by interface alone would hang
  // sparkjr's port off sparky's chart — and, now that a port decides the
  // division, would also move the wrong node's link into Fabric.
  const { names, columns } = dataset({
    [`sparky/enP7s7/${RX}`]: [10],
    [`sparkjr/enP7s7/${RX}`]: [10],
  });
  const rdma = ports([portRow('sparkjr', 'roceX', 'enP7s7', [0])]);
  const grid = buildGrid(names, columns, ['sparky', 'sparkjr'], null, false, rdma, new Set());
  const fab = grid.groups.find((g) => g.key === 'fabric');
  const mgmt = grid.groups.find((g) => g.key === 'management');
  assert.deepEqual(
    fab.charts.map((c) => `${c.link.node} ${c.metric.label}`),
    ['sparkjr enP7s7', 'sparkjr roceX port 1'],
  );
  assert.deepEqual(
    mgmt.charts.map((c) => `${c.link.node} ${c.metric.label}`),
    ['sparky enP7s7'],
  );
});

// ---------------------------------------------------------- the division

const FABRIC = new Set([linkKey('sparky', 'enP2p1s0f0np0')]);

test('a paired interface goes to Fabric and an unpaired one to Management', () => {
  const { names, columns } = dataset({
    [`sparky/enP2p1s0f0np0/${RX}`]: [10],
    [`sparky/enP7s7/${RX}`]: [10],
  });
  const grid = buildGrid(names, columns, ['sparky'], null, false, [], FABRIC);
  assert.deepEqual(grid.groups.map((g) => g.key), ['fabric', 'management']);
  assert.equal(divisionOf(grid, 'enP2p1s0f0np0'), 'fabric');
  assert.equal(divisionOf(grid, 'enP7s7'), 'management');
});

test('fabric comes first — it is the question the cluster exists for', () => {
  const { names, columns } = dataset({
    // Alphabetically enP7s7 sorts after, so this is not just insertion order.
    [`sparky/enP7s7/${RX}`]: [10],
    [`sparky/enP2p1s0f0np0/${RX}`]: [10],
  });
  const grid = buildGrid(names, columns, ['sparky'], null, false, [], FABRIC);
  assert.equal(grid.groups[0].key, 'fabric');
});

test('an empty division is not drawn', () => {
  // A cluster with no RoCE at all gets one heading, not one and an empty frame.
  const { names, columns } = dataset({ [`sparky/enP7s7/${RX}`]: [10] });
  const grid = buildGrid(names, columns, ['sparky'], null, false, [], new Set());
  assert.deepEqual(grid.groups.map((g) => g.key), ['management']);
});

test('a port hanging off an interface makes it fabric whatever the set says', () => {
  // The pairing IS the definition, and a port attached to the link is that
  // pairing observed directly — so an empty fabric set cannot override it.
  const { names, columns } = dataset({ [`sparky/enP2p1s0f1np1/${RX}`]: [10] });
  const rdma = ports([portRow('sparky', 'roceP2p1s0f1', 'enP2p1s0f1np1', [1, 0])]);
  const grid = buildGrid(names, columns, ['sparky'], null, false, rdma, new Set());
  assert.equal(divisionOf(grid, 'enP2p1s0f1np1'), 'fabric');
  assert.equal(divisionOf(grid, 'roceP2p1s0f1 port 1'), 'fabric');
});

test('a port chart with no pairing still lands in Fabric', () => {
  // It is an RDMA port by construction. Management would be actively wrong.
  const { names, columns } = dataset({ [`sparky/enP7s7/${RX}`]: [10] });
  const rdma = ports([portRow('sparky', 'roceP2p1s0f1', '', [1, 0])]);
  const grid = buildGrid(names, columns, ['sparky'], null, false, rdma, new Set());
  assert.equal(divisionOf(grid, 'roceP2p1s0f1 port 1'), 'fabric');
  assert.equal(divisionOf(grid, 'enP7s7'), 'management');
});

test("a fault chart stays in its interface's division", () => {
  const { names, columns } = dataset({
    [`sparky/enP2p1s0f0np0/${RX}`]: [10],
    [`sparky/enP2p1s0f0np0/${ERRORS}`]: [3],
  });
  const grid = buildGrid(names, columns, ['sparky'], null, false, [], FABRIC);
  assert.equal(grid.groups.length, 1);
  assert.deepEqual(grid.groups[0].charts.map((c) => c.metric.label), [
    'enP2p1s0f0np0',
    'enP2p1s0f0np0 faults',
  ]);
});

test('every division names what it means', () => {
  const { names, columns } = dataset({
    [`sparky/enP2p1s0f0np0/${RX}`]: [10],
    [`sparky/enP7s7/${RX}`]: [10],
  });
  for (const g of buildGrid(names, columns, ['sparky'], null, false, [], FABRIC).groups) {
    assert.ok(g.label && g.note, `${g.key} has no heading or no definition`);
  }
});

// ------------------------------------------------------- the overview table

/** Rows for one link, keyed the way `dataset` keys everything else. */
const rowsFor = (
  spec,
  { rdma = [], fabric = new Set(), nodes = ['sparky'], live = new Map() } = {},
) => {
  const { names, columns } = dataset(spec);
  return buildRows(names, columns, nodes, null, rdma, fabric, live).flatMap((d) =>
    d.rows.map((r) => ({ ...r, division: d.key })),
  );
};
const only = (spec, opts) => rowsFor(spec, opts)[0];

test('a row sums the two directions — one wire, one line', () => {
  const r = only({ [`sparky/eth0/${RX}`]: [10, 20], [`sparky/eth0/${TX}`]: [1, 2] });
  assert.deepEqual(r.series, [11, 22]);
  assert.equal(r.peak, 22);
  assert.equal(r.now, 22);
});

test('the newest sample is the last one that EXISTS', () => {
  // The freshest bucket is routinely still null. Reading the last element would
  // report every link as idle for the length of one step.
  const r = only({ [`sparky/eth0/${RX}`]: [10, 20, null] });
  assert.equal(r.now, 20);
});

test('null in both directions is a gap, not a zero', () => {
  // null + 0 is 0 in JavaScript, which would draw a sample that never existed.
  const r = only({ [`sparky/eth0/${RX}`]: [10, null], [`sparky/eth0/${TX}`]: [1, null] });
  assert.deepEqual(r.series, [11, null]);
});

test('a link that was down at any point is the top tier', () => {
  const r = only({
    [`sparky/eth0/${RX}`]: [10, 10],
    [`sparky/eth0/${LINK_UP}`]: [1, 0],
  });
  assert.equal(r.up, false);
  assert.equal(r.tier, TIER_STATE);
  assert.equal(r.why, 'link down');
});

test('errors outrank drops', () => {
  // A drop is backpressure, an error is usually a cable. Same distinction the
  // two fault lines are kept apart for.
  const r = only({
    [`sparky/eth0/${RX}`]: [10],
    [`sparky/eth0/${ERRORS}`]: [1],
    [`sparky/eth0/${DROPS}`]: [9],
  });
  assert.equal(r.why, 'errors');
});

/** A window that sits at `base` and spikes to `peak` once — the shape of a
 *  real burst, and the only shape peak/mean can actually detect. A two-sample
 *  series cannot exceed a ratio of 2 no matter what it does. */
const spike = (base, peak, n = 60) => {
  const v = new Array(n).fill(base);
  v[Math.floor(n / 2)] = peak;
  return v;
};

test('a bursty link outranks a steady one, and a steady one is not flagged', () => {
  const bursty = only({ [`sparky/eth0/${RX}`]: spike(1, 100) });
  const steady = only({ [`sparky/eth0/${RX}`]: spike(10, 11) });
  assert.equal(bursty.tier, TIER_BURST);
  assert.equal(bursty.why, 'bursty');
  assert.equal(steady.tier, TIER_STEADY);
  assert.equal(steady.why, '');
  assert.ok(bursty.burst >= BURST_RATIO && steady.burst < BURST_RATIO);
});

test('the measured tiers land on the right side of the threshold', () => {
  // The three groups this cluster actually produced over 24h, at the peak and
  // mean that were measured. The threshold has to separate them or it was
  // picked from thin air.
  const asMeasured = (mean, peak, n = 288) => {
    // Solve for the base that yields the measured mean once the spike is in.
    const base = (mean * n - peak) / (n - 1);
    const v = new Array(n).fill(base);
    v[n >> 1] = peak;
    return v;
  };
  const busy = only({ [`sparky/eth0/${RX}`]: asMeasured(28547907, 580223842) });
  const steady = only({ [`sparky/eth0/${RX}`]: asMeasured(401407, 730416) });
  const flat = only({ [`sparky/eth0/${RX}`]: asMeasured(248, 283) });
  assert.ok(busy.burst > 15, `busy measured ${busy.burst}`);
  assert.ok(steady.burst < 2, `steady measured ${steady.burst}`);
  assert.ok(flat.burst < 2, `flat measured ${flat.burst}`);
  assert.equal(busy.tier, TIER_BURST);
  assert.equal(steady.tier, TIER_STEADY);
  assert.equal(flat.tier, TIER_STEADY);
});

test('a short series cannot fake a burst', () => {
  // peak/mean is bounded by the sample count: with n points the ceiling is n,
  // and with two it is 2. Real windows carry 60-170 samples, so the threshold
  // is reachable — but a fixture of two numbers can never trip it, which is
  // worth pinning because it made the first version of the test above wrong.
  const r = only({ [`sparky/eth0/${RX}`]: [1, 1000] });
  assert.ok(r.burst <= 2, `two samples produced a ratio of ${r.burst}`);
});

test('a port down beats a port that only flapped — worst wins', () => {
  const rdma = ports([
    portRow('sparky', 'roceA', 'eth0', [1, 1]),
    portRow('sparky', 'roceB', 'eth0', [0, 0]),
    portRow('sparky', 'roceC', 'eth0', [1, 0]),
  ]);
  const r = only({ [`sparky/eth0/${RX}`]: [10, 10] }, { rdma });
  assert.equal(r.port, 'down');
  assert.equal(r.why, 'port down');
});

test('a cable whose ports are all healthy reads up', () => {
  const rdma = ports([portRow('sparky', 'roceA', 'eth0', [1, 1])]);
  const r = only({ [`sparky/eth0/${RX}`]: [10, 10] }, { rdma });
  assert.equal(r.port, 'up');
  assert.equal(r.tier, TIER_STEADY);
});

test('a link with no RoCE has no port state at all', () => {
  // Not "up". An interface with no RDMA device has nothing to report, and a
  // column saying "up" would invent a port.
  assert.equal(only({ [`sparky/eth0/${RX}`]: [10] }).port, null);
});

test('volume is the last key, never the first', () => {
  // On its own it ranks a busy management port above every fabric link, every
  // time — the opposite of what anyone opened this card for.
  const rows = rowsFor({
    [`sparky/busy/${RX}`]: [1000, 1000],
    [`sparky/quiet/${RX}`]: [1, 1],
    [`sparky/quiet/${DROPS}`]: [0, 5],
  });
  assert.deepEqual(rows.map((r) => r.iface), ['quiet', 'busy']);
});

test('inside a tier, the busiest comes first', () => {
  const rows = rowsFor({
    [`sparky/small/${RX}`]: [5, 5],
    [`sparky/big/${RX}`]: [900, 900],
  });
  assert.deepEqual(rows.map((r) => r.iface), ['big', 'small']);
});

test('burst noise does not decide the order of steady links', () => {
  /* MEASURED ON THE DEPLOYED PAGE. Sorting every tier by burst put the four
     steady RoCE links in ASCENDING order of throughput — 74, 76, 77, 79 kb/s —
     because their burst ratios were 1.12 against 1.09 and that decided it. Two
     links are never bursty to the same four decimals, so volume never got a
     turn and the rows looked shuffled. Burst is a sort key only where it means
     something: inside the bursty tier. */
  const rows = rowsFor({
    // `quiet` is a hair burstier; `loud` carries twelve times the traffic.
    [`sparky/quiet/${RX}`]: [9, 10, 9, 11],
    [`sparky/loud/${RX}`]: [120, 120, 120, 121],
  });
  assert.equal(rows[0].tier, rows[1].tier, 'fixture should put both in one tier');
  assert.ok(rows.find((r) => r.iface === 'quiet').burst > rows.find((r) => r.iface === 'loud').burst);
  assert.deepEqual(rows.map((r) => r.iface), ['loud', 'quiet']);
});

test('inside the bursty tier, the one that moved most comes first', () => {
  const rows = rowsFor({
    [`sparky/spikier/${RX}`]: spike(1, 500),
    [`sparky/spiky/${RX}`]: spike(1, 50),
  });
  assert.deepEqual(rows.map((r) => r.iface), ['spikier', 'spiky']);
});

test('the order is total — no pair ever compares equal', () => {
  // Two identical links on two nodes must still have a stable order, or the
  // table reshuffles on every refresh.
  const a = { key: 'a', node: 'n1', iface: 'eth0', tier: 2, burst: 1, peak: 1 };
  const b = { key: 'b', node: 'n2', iface: 'eth0', tier: 2, burst: 1, peak: 1 };
  assert.ok(byImportance(a, b) < 0);
  assert.ok(byImportance(b, a) > 0);
});

test('rows split into the same divisions the charts use', () => {
  const divs = buildRows(
    ...(() => { const d = dataset({
      [`sparky/fab/${RX}`]: [10], [`sparky/mgmt/${RX}`]: [10] });
      return [d.names, d.columns]; })(),
    ['sparky'], null, [], new Set([linkKey('sparky', 'fab')]),
  );
  assert.deepEqual(divs.map((d) => d.key), ['fabric', 'management']);
  assert.deepEqual(divs[0].rows.map((r) => r.iface), ['fab']);
});

// ------------------------------------------------------------- sparklines

test('a sparkline is a path, and a flat line sits on the baseline', () => {
  const d = sparkPath([0, 0, 0], 60, 10);
  assert.ok(d.startsWith('M'));
  // height 10 -> every y is 10.0, the bottom.
  assert.ok(/10\.0/.test(d) && !/ 0\.0/.test(d), `flat line is not on the baseline: ${d}`);
});

test('a gap breaks the path instead of being drawn through', () => {
  const d = sparkPath([1, null, 1], 60, 10);
  assert.equal((d.match(/M/g) || []).length, 2, `expected two subpaths: ${d}`);
});

test('downsampling keeps the spike clear of the baseline', () => {
  /* MAX per bucket, not mean. The first version of this test asserted only that
     the peak reached the top of the chart, which BOTH do — the scale is
     relative to the downsampled points, so whatever the tallest bucket is ends
     up at y=0. It passed against an averaging implementation.

     What actually separates them is a narrow spike over a HIGH baseline. Ten
     samples per bucket, baseline 100, one sample at 500: max draws the bucket
     at 500 and the baseline sits at a fifth of the height; mean draws it at 140
     and the baseline rises to three quarters, which is a ripple. */
  const values = new Array(240).fill(100);
  values[100] = 500;
  const ys = sparkPath(values, 100, 10, 24)
    .split(/[ML]/)
    .filter(Boolean)
    .map((pt) => parseFloat(pt.trim().split(' ')[1]));

  assert.ok(ys.includes(0), `the peak did not reach the top: ${ys}`);
  const baseline = ys.filter((y) => y > 0);
  assert.ok(
    baseline.every((y) => y >= 7),
    `the baseline rose off the floor, so the spike was averaged away: ${ys}`,
  );
});

test('no data is an empty path, not a broken one', () => {
  assert.equal(sparkPath([], 60, 10), '');
  assert.equal(sparkPath([null, null], 60, 10), '');
});

// ------------------------------------------------------------------- scale

test('190 links across 32 nodes build, rank and divide', () => {
  // The claim the whole table exists for, checked where it can be: a browser
  // cannot reach 32 nodes on this cluster.
  const spec = {};
  const nodes = [];
  const fabric = new Set();
  for (let n = 0; n < 32; n++) {
    const node = `gx10-${String(n).padStart(2, '0')}`;
    nodes.push(node);
    for (const iface of ['enP2p1s0f0np0', 'enP2p1s0f1np1', 'enp1s0f0np0', 'enp1s0f1np1']) {
      spec[`${node}/${iface}/${RX}`] = [10, 10 + n];
      fabric.add(linkKey(node, iface));
    }
    spec[`${node}/enP7s7/${RX}`] = [100, 200];
    spec[`${node}/wlP9s9/${RX}`] = [0, 0];
  }
  // One link in the middle of the pack goes bad.
  spec[`gx10-17/enp1s0f1np1/${DROPS}`] = [0, 12];

  const { names, columns } = dataset(spec);
  const divs = buildRows(names, columns, nodes, null, [], fabric);
  const all = divs.flatMap((d) => d.rows);

  assert.equal(all.length, 32 * 6, `expected 192 rows, got ${all.length}`);
  assert.deepEqual(divs.map((d) => d.key), ['fabric', 'management']);
  assert.equal(divs[0].rows.length, 32 * 4);
  assert.equal(divs[1].rows.length, 32 * 2);
  // The one bad link is the first row of its division, out of 128.
  assert.equal(divs[0].rows[0].iface, 'enp1s0f1np1');
  assert.equal(divs[0].rows[0].node, 'gx10-17');
  assert.equal(divs[0].rows[0].why, 'drops');
});

// --------------------------------------------------------- live pairing

test('a port with no exported pairing is filled in from the live snapshot', () => {
  // `rdma_port_info.interface` shipped in AC1c and only reaches Prometheus once
  // a node's stack is redeployed. Until then the roce column would read "—" for
  // every fabric link — the one column the division exists to explain.
  const paired = pairPorts(ports([portRow('sparky', 'roceA', '', [1, 0])]), [
    { node: 'sparky', device: 'roceA', iface: 'enP2p1s0f0np0' },
  ]);
  assert.equal(paired[0].iface, 'enP2p1s0f0np0');
});

test('the exported pairing wins over the live one', () => {
  // The metric is contemporaneous with the samples; the live answer is now.
  const paired = pairPorts(ports([portRow('sparky', 'roceA', 'from-metric', [1, 0])]), [
    { node: 'sparky', device: 'roceA', iface: 'from-live' },
  ]);
  assert.equal(paired[0].iface, 'from-metric');
});

test('a live pairing on another node does not leak across', () => {
  const paired = pairPorts(ports([portRow('sparky', 'roceA', '', [1, 0])]), [
    { node: 'sparketa', device: 'roceA', iface: 'eth9' },
  ]);
  assert.equal(paired[0].iface, '');
});

test('a filled pairing reaches the row roce column', () => {
  const rdma = pairPorts(ports([portRow('sparky', 'roceA', '', [1, 0])]), [
    { node: 'sparky', device: 'roceA', iface: 'eth0' },
  ]);
  const r = only({ [`sparky/eth0/${RX}`]: [10, 10] }, { rdma });
  assert.equal(r.port, 'flapped');
  assert.equal(r.why, 'port flapped');
});

// --------------------------------------- facts with no time series

test('speed and the monitored flag come off the live snapshot', () => {
  // Both moved here when the live Network card stopped drawing a second
  // interface table. Neither is a rate, so applying today's answer to a window
  // is sound in a way that applying today's throughput would not be.
  const live = new Map([
    [linkKey('sparky', 'eth0'), { speedMbps: 200000, monitored: false }],
  ]);
  const r = only({ [`sparky/eth0/${RX}`]: [10] }, { live });
  assert.equal(r.speedMbps, 200000);
  assert.equal(r.monitored, false);
});

test('a link the live feed has never mentioned is monitored, with no speed', () => {
  // An agent that predates the `monitored` flag watches everything, which is
  // what the field means — so the default has to be true, not false. And absent
  // is not zero: every wifi port here reports no speed at all.
  const r = only({ [`sparky/eth0/${RX}`]: [10] });
  assert.equal(r.monitored, true);
  assert.equal(r.speedMbps, null);
});

test('the monitored flag does not filter anything', () => {
  // Reusing it to decide what the card DRAWS was rejected when the divisions
  // shipped: it exists to decide what alerts, and a flag serving two purposes
  // is how a flag starts lying. Reporting it is the opposite.
  const live = new Map([
    [linkKey('sparky', 'quiet'), { speedMbps: 10000, monitored: false }],
  ]);
  const rows = rowsFor(
    { [`sparky/quiet/${RX}`]: [10], [`sparky/loud/${RX}`]: [900] },
    { live },
  );
  assert.equal(rows.length, 2, 'an unmonitored link must still be drawn');
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
