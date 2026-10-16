/* The thermal ranking, actually executed.
 *
 * `thermal.ts` is plain TypeScript with no runes and no DOM, and it is almost
 * entirely ordering rules — which sensor headlines, what sorts above what, how
 * a sensor with no limit is treated. A regex over the source would pass on an
 * implementation that got every one of them backwards.
 *
 * Run from tests/test_thermal_js.py, which transpiles this with the esbuild
 * Vite already brings and runs it under node.
 */

import assert from 'node:assert/strict';

import {
  byHeadroom,
  groupRows,
  hottest,
  tempRows,
  tightest,
  groupRows as group,
  combineRows,
  headroomState,
  thermalItems,
  FLOOR_C,
} from '../../frontend/src/lib/thermal.ts';

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

/** A node snapshot carrying just the sensors. */
const node = (id, sensors) => ({
  node_id: id,
  temperatures: sensors.map(([domain, sensor, celsius, limit_c = null]) => ({
    domain,
    sensor,
    celsius,
    limit_c,
  })),
});

/** The measured GB10 layout: 7 package zones, nvme, four NIC asics, wifi. */
const gb10 = (id, { hot = 60 } = {}) =>
  node(id, [
    ['package', 'zone0', hot, 104.8],
    ['package', 'zone1', 45.1, 104.8],
    ['storage', 'nvme nvme0 Composite', 42.85, 84.85],
    ['storage', 'nvme nvme0 Sensor 1', 44.85, null],
    ['network', 'mlx5 0000:01:00.0 asic', 52.0, 105.0],
    ['wireless', 'mt7925_phy0 phy0 temp1', 42.0, null],
    ['gpu', 'gpu', 72.0, 90.0],
  ]);

// ------------------------------------------------------------------ rows

test('a sensor with a limit gets headroom; one without gets none', () => {
  const rows = tempRows([gb10('sparky')]);
  const zone = rows.find((r) => r.sensor === 'zone0');
  const wifi = rows.find((r) => r.domain === 'wireless');
  assert.equal(zone.headroomC.toFixed(1), '44.8');
  // Absent is not unlimited, and it is certainly not zero.
  assert.equal(wifi.headroomC, null);
});

test('rows are keyed per node, so two nodes do not collide', () => {
  const rows = tempRows([gb10('sparky'), gb10('sparketa')]);
  assert.equal(new Set(rows.map((r) => r.key)).size, rows.length);
});

test('a node reporting no sensors contributes no rows', () => {
  assert.deepEqual(tempRows([{ node_id: 'old', temperatures: [] }]), []);
  assert.deepEqual(tempRows([{ node_id: 'older' }]), []);
});

// -------------------------------------------------------------- headlines

test('the headline is the hottest sensor, not the GPU', () => {
  /* THE WHOLE POINT. Measured over 24h on this cluster, zone0 peaked at 95.4 C
     while the GPU read 72.0 C at the same instant. A headline that took the
     GPU would be 23 degrees low. */
  const rows = tempRows([gb10('sparky', { hot: 95.4 })]);
  assert.equal(hottest(rows).sensor, 'zone0');
  assert.equal(hottest(rows).celsius, 95.4);
});

test('hottest and closest-to-limit are different sensors', () => {
  /* A 52 C NIC rated to 105 is cooler and safer than an 85 C GPU rated to 90.
     Conflating them is exactly what a single "system temperature" does. */
  const rows = tempRows([
    node('n', [
      ['package', 'zone0', 88.0, 104.8], // hottest, 16.8 to go
      ['gpu', 'gpu', 86.0, 90.0], // cooler, 4.0 to go
    ]),
  ]);
  assert.equal(hottest(rows).sensor, 'zone0');
  assert.equal(tightest(rows).sensor, 'gpu');
});

test('a sensor with no limit can never be the tightest', () => {
  const rows = tempRows([node('n', [['wireless', 'phy0', 42.0, null]])]);
  assert.equal(tightest(rows), null, 'an unmeasurable sensor claimed the worst margin');
});

test('with nothing reported both headlines are null, not zero', () => {
  assert.equal(hottest([]), null);
  assert.equal(tightest([]), null);
});

// ----------------------------------------------------------------- order

test('least headroom first, across domains', () => {
  const rows = tempRows([
    node('n', [
      ['network', 'nic', 52.0, 105.0], // 53.0 left
      ['gpu', 'gpu', 85.0, 90.0], //  5.0 left
      ['package', 'zone0', 88.0, 104.8], // 16.8 left
    ]),
  ]).sort(byHeadroom);
  assert.deepEqual(rows.map((r) => r.sensor), ['gpu', 'zone0', 'nic']);
});

test('sorting by temperature would give the opposite answer', () => {
  /* Pinned deliberately: it is the reason headroom exists as a concept here.
     By degrees the package zone leads and the GPU looks second; by margin the
     GPU is five degrees from shutdown and the zone has seventeen. */
  const rows = tempRows([
    node('n', [
      ['package', 'zone0', 88.0, 104.8],
      ['gpu', 'gpu', 85.0, 90.0],
    ]),
  ]);
  const byTemp = [...rows].sort((a, b) => b.celsius - a.celsius).map((r) => r.sensor);
  const byMargin = [...rows].sort(byHeadroom).map((r) => r.sensor);
  assert.deepEqual(byTemp, ['zone0', 'gpu']);
  assert.deepEqual(byMargin, ['gpu', 'zone0']);
});

test('a sensor with no limit sorts LAST, never first', () => {
  /* No known margin is not the same as no margin left. Putting an unmeasurable
     wifi radio above a GPU five degrees from shutdown would be the exact
     inversion this ranking exists to prevent. */
  const rows = tempRows([
    node('n', [
      ['wireless', 'phy0', 42.0, null],
      ['gpu', 'gpu', 85.0, 90.0],
    ]),
  ]).sort(byHeadroom);
  assert.deepEqual(rows.map((r) => r.sensor), ['gpu', 'phy0']);
});

test('two limitless sensors fall back to temperature', () => {
  const rows = tempRows([
    node('n', [
      ['wireless', 'cool', 40.0, null],
      ['wireless', 'warm', 60.0, null],
    ]),
  ]).sort(byHeadroom);
  assert.deepEqual(rows.map((r) => r.sensor), ['warm', 'cool']);
});

test('the order is total — no pair ever compares equal', () => {
  // Identical sensors on two nodes must still have a stable order, or the
  // table reshuffles on every poll.
  const rows = tempRows([
    node('a', [['package', 'zone0', 60.0, 104.8]]),
    node('b', [['package', 'zone0', 60.0, 104.8]]),
  ]);
  assert.ok(byHeadroom(rows[0], rows[1]) < 0);
  assert.ok(byHeadroom(rows[1], rows[0]) > 0);
});

// ---------------------------------------------------------------- groups

test('domains are drawn hottest-first, not alphabetically', () => {
  const g = group(tempRows([gb10('sparky')])).map((x) => x.key);
  assert.deepEqual(g, ['package', 'gpu', 'storage', 'network', 'wireless']);
  assert.notDeepEqual(g, [...g].sort());
});

test('a domain with no sensors is not drawn', () => {
  /* sparky reports no NIC chips at all. An empty "Network" heading would read
     as a fault rather than as a machine without that hardware. */
  const rows = tempRows([node('sparky', [['package', 'zone0', 60.0, 104.8]])]);
  assert.deepEqual(groupRows(rows).map((x) => x.key), ['package']);
});

test('a domain this build has never heard of is still drawn', () => {
  // Same rule as `other` in the agent: a sensor nobody anticipated is the one
  // most worth seeing.
  const rows = tempRows([node('n', [['plasma_conduit', 'weird0', 70.0, 90.0]])]);
  const g = groupRows(rows);
  assert.equal(g.length, 1);
  assert.equal(g[0].key, 'plasma_conduit');
  assert.ok(g[0].note, 'an unrecognised domain got no explanation');
});

test('every group is ranked by headroom internally', () => {
  const rows = tempRows([
    node('n', [
      ['package', 'cool', 50.0, 104.8],
      ['package', 'hot', 100.0, 104.8],
    ]),
  ]);
  assert.deepEqual(groupRows(rows)[0].rows.map((r) => r.sensor), ['hot', 'cool']);
});

test('every group names what it contains', () => {
  for (const g of groupRows(tempRows([gb10('sparky')]))) {
    assert.ok(g.label && g.note, `${g.key} has no label or no explanation`);
  }
});

// ----------------------------------------------------------------- scale

test('32 nodes of sensors group and rank', () => {
  const nodes = [];
  for (let i = 0; i < 32; i++) nodes.push(gb10(`gx10-${String(i).padStart(2, '0')}`));
  // One node's package zone is about to trip.
  nodes[17].temperatures[0].celsius = 103.0;

  const rows = tempRows(nodes);
  assert.equal(rows.length, 32 * 7);
  const g = groupRows(rows);
  assert.deepEqual(g.map((x) => x.key), ['package', 'gpu', 'storage', 'network', 'wireless']);
  assert.equal(g[0].rows.length, 32 * 2);
  assert.equal(g[0].rows[0].node, 'gx10-17', 'the tripping node did not head its domain');
  assert.equal(tightest(rows).node, 'gx10-17');
});

/* --- BD: combined readings and gauges ------------------------------ */

const pkg = () =>
  tempRows([
    node('sparky', [
      ['package', 'zone0', 95.4, 104.8],
      ['package', 'zone1', 58.0, 104.8],
      ['package', 'zone2', 58.6, 104.8],
    ]),
    node('sparketa', [['package', 'zone0', 50.0, 104.8], ['package', 'zone1', 48.0, 104.8]]),
  ]);

test('hottest per node is the hottest sensor, one row a node', () => {
  const r = combineRows(pkg(), 'hottest');
  assert.equal(r.length, 2);
  const s = r.find((x) => x.node === 'sparky');
  assert.equal(s.celsius, 95.4);
  assert.equal(s.count, 3);
  assert.match(s.sensor, /hottest of 3: zone0/);
});

test('an average is the mean, and its state still comes from the tightest sensor', () => {
  const s = combineRows(pkg(), 'average').find((x) => x.node === 'sparky');
  assert.ok(Math.abs(s.celsius - (95.4 + 58.0 + 58.6) / 3) < 1e-9);
  // 70.7 on average would look comfortable against 104.8; zone0 has 9.4 left.
  assert.ok(Math.abs(s.headroomC - 9.4) < 1e-9);
  assert.equal(s.tightestSensor, 'zone0');
  assert.equal(headroomState(s.headroomC), 'warning', 'an average hid the hot zone');
  assert.equal(s.hottestSensor, 'zone0');
});

test('combined rows rank by headroom, least first', () => {
  const r = combineRows(pkg(), 'average');
  assert.deepEqual(r.map((x) => x.node), ['sparky', 'sparketa']);
});

test('headroom states match the table: 5 or less critical, 15 or less warning', () => {
  assert.equal(headroomState(5), 'critical');
  assert.equal(headroomState(5.1), 'warning');
  assert.equal(headroomState(15), 'warning');
  assert.equal(headroomState(15.1), 'ok');
  assert.equal(headroomState(null), 'ok');
});

test('a dial runs from the floor to the tightest limit, with the card\'s two marks', () => {
  const items = thermalItems(combineRows(pkg(), 'hottest'), () => '#000');
  const s = items.find((x) => x.id === 'sparky');
  assert.ok(Math.abs(s.fraction - (95.4 - FLOOR_C) / (104.8 - FLOOR_C)) < 1e-9);
  assert.deepEqual(s.scale, ['40°', '105°']);
  assert.ok(Math.abs(s.bands[0] - (104.8 - 15 - FLOOR_C) / (104.8 - FLOOR_C)) < 1e-9);
  assert.ok(Math.abs(s.bands[1] - (104.8 - 5 - FLOOR_C) / (104.8 - FLOOR_C)) < 1e-9);
  assert.equal(s.state, 'warning');
  assert.equal(s.value, '95.4°');
  assert.deepEqual(items.map((x) => x.id), ['sparketa', 'sparky'], 'dials are in node order, not by heat');
});

test('an average dial says so, and names the hottest sensor in its title', () => {
  const s = thermalItems(combineRows(pkg(), 'average'), () => '#000').find((x) => x.id === 'sparky');
  assert.equal(s.unit, 'avg °C');
  assert.match(s.title, /hottest zone0 95\.4°/);
  assert.match(s.title, /zone0, 9\.4° left/);
});

test('a group with no stated limit gets an empty dial, not a made-up scale', () => {
  const rows = tempRows([node('sparky', [['wireless', 'phy0', 42.0]])]);
  const [s] = thermalItems(combineRows(rows, 'hottest'), () => '#000');
  assert.equal(s.fraction, 0);
  assert.equal(s.bands, null);
  assert.equal(s.scale[1], '—');
  assert.equal(s.state, 'ok');
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
