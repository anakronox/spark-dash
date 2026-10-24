/* System Activity's gauges (roadmap BA), actually executed.
 *
 * Run from tests/test_gauges_js.py, which transpiles this with the esbuild
 * Vite already brings and runs it under node.
 */

import assert from 'node:assert/strict';

import {
  PROMETHEUS_ONLY,
  STATE_WORD,
  arcPath,
  bandsOf,
  ceilingOf,
  dialFit,
  formatReading,
  kindOf,
  peakLabel,
  liveValue,
  stateOf,
  capacityLabel,
  linkItems,
  linkLoads,
  systemItems,
} from '../../frontend/src/lib/gauges.ts';
import { METRICS } from '../../frontend/src/lib/history.ts';

let passed = 0;
let failed = 0;
const test = (name, fn) => { try { fn(); passed++; } catch (e) { failed++; console.error(`FAIL ${name}\n${e.message}\n`); } };

const spec = (key) => METRICS.find((m) => m.key === key);

const node = (over = {}) => ({
  node_id: 'sparky',
  up: true,
  gpu: { util_pct: 42, temp_c: 84.5, power_w: 38, clock_mhz: 2411, clock_state: 'PASS' },
  memory: { total_bytes: 200, available_bytes: 110, used_bytes: 90, swap_used_bytes: 0, unified: true },
  cpu: { util_pct: 7, temp_c: 70, load_avg_1m: 1, active_cores: 20 },
  psi: { some_avg10: 3, some_avg60: 2, full_avg10: 0.5, full_avg60: 0, state: 'LOW' },
  temp_bands: { gpu_warning_c: 82, gpu_critical_c: 86, gpu_source: 'nvml-slowdown', cpu_warning_c: 92.8, cpu_critical_c: 98.8, cpu_source: 'acpi-critical-trip' },
  temperatures: [
    { domain: 'package', sensor: 'zone0', celsius: 87.1, limit_c: 104.8 },
    { domain: 'package', sensor: 'zone5', celsius: 80, limit_c: 104.8 },
    { domain: 'gpu', sensor: 'gpu', celsius: 84.5, limit_c: 90 },
    { domain: 'storage', sensor: 'nvme0', celsius: 58.9, limit_c: 84.85 },
    { domain: 'network', sensor: 'mlx5 asic', celsius: 56, limit_c: 105 },
    { domain: 'wireless', sensor: 'phy0', celsius: 44, limit_c: null },
  ],
  runtimes: {
    llama_cpp: [{ generation_tokens_per_sec: 12, models: [{ prompt_tokens_per_sec: 100 }, { prompt_tokens_per_sec: null }] }],
    vllm: [],
    sglang: [],
    tensorfold: [{ generation_tokens_per_sec: 31, prompt_tokens_per_sec: 400 }],
  },
  ...over,
});

test('every charted metric is either live or served by /api/latest', () => {
  const n = node();
  for (const m of METRICS) {
    if (PROMETHEUS_ONLY.has(m.key)) continue;
    assert.notEqual(liveValue(m.key, n), null, `${m.key} has no live reading`);
  }
});

test('live readings are the quantities the charts plot', () => {
  const n = node();
  assert.equal(liveValue('memory_used_percent', n), 45);
  assert.equal(liveValue('tokens_per_second', n), 43); // routers + engines, decode only
  assert.equal(liveValue('prompt_tokens_per_second', n), 500);
  assert.equal(liveValue('system_temperature', n), 87.1); // the hottest, GPU included
  assert.equal(liveValue('package_temperature', n), 87.1);
  assert.equal(liveValue('storage_temperature', n), 58.9);
  // least margin to a sensor's OWN limit: the GPU's 5.5, not the package's 17.7
  assert.equal(liveValue('temperature_headroom', n), 90 - 84.5);
});

test('a down node and a missing sensor read as nothing, never zero', () => {
  assert.equal(liveValue('gpu_utilization', node({ up: false })), null);
  const noNic = node();
  noNic.temperatures = noNic.temperatures.filter((t) => t.domain !== 'network');
  assert.equal(liveValue('network_temperature', noNic), null);
  assert.equal(liveValue('gpu_power', node({ gpu: null })), null);
});

test('dials use the full-axis scale; metrics with no ceiling are tiles', () => {
  assert.equal(ceilingOf(spec('gpu_utilization')), 100);
  assert.equal(ceilingOf(spec('gpu_temperature')), 100);
  assert.equal(ceilingOf(spec('gpu_power')), 300);
  assert.equal(ceilingOf(spec('cpu_clock')), 3500);
  // No ceiling of their own: dials scaled to each node's 24h peak (BV).
  for (const k of ['tokens_per_second', 'prompt_tokens_per_second', 'swap_io', 'temperature_headroom']) {
    assert.equal(ceilingOf(spec(k)), null, k);
    assert.equal(kindOf(spec(k)), 'dial', k);
  }
});

test('a peak-scaled dial reads against its node\'s 24h peak', () => {
  const n = node();
  const v = liveValue('tokens_per_second', n);
  const [it] = systemItems(spec('tokens_per_second'), [n], () => 'c', undefined, { [n.node_id]: v * 4 });
  assert.equal(it.fraction, 0.25);
  assert.equal(it.scale[1], peakLabel(v * 4));
  // a reading past a minute-old peak fills the dial, never overruns it
  const [over] = systemItems(spec('tokens_per_second'), [n], () => 'c', undefined, { [n.node_id]: v / 2 });
  assert.equal(over.fraction, 1);
  // no peak and nothing now: no scale, an empty arc
  const idle = node({ runtimes: { llama_cpp: [], vllm: [], sglang: [], tensorfold: [] } });
  const [none] = systemItems(spec('tokens_per_second'), [idle], () => 'c', undefined, {});
  assert.equal(none.fraction, 0);
  assert.equal(none.scale[1], '');
  // from Prometheus, with its peak
  const [swap] = systemItems(spec('swap_io'), [node()], () => 'c', { [node().node_id]: 30 }, { [node().node_id]: 120 });
  assert.equal(swap.fraction, 0.25);
  assert.equal(swap.scale[1], '120');
});

test('peak labels stay short', () => {
  assert.deepEqual([0.5, 9.94, 47.3, 820, 1250, 12400].map(peakLabel), ['0.5', '9.9', '47', '820', '1.3k', '12k']);
});

test('bands only where the hardware states a warning and a critical', () => {
  const n = node();
  assert.deepEqual(bandsOf('gpu_temperature', n), [82, 86]);
  assert.deepEqual(bandsOf('package_temperature', n), [92.8, 98.8]);
  assert.equal(bandsOf('storage_temperature', n), null);
  assert.equal(bandsOf('gpu_temperature', node({ temp_bands: null })), null);
});

test('state is strictly above the band, as the node card judges it', () => {
  const b = [82, 86];
  assert.equal(stateOf(82, b), 'ok');
  assert.equal(stateOf(82.1, b), 'warning');
  assert.equal(stateOf(86, b), 'warning');
  assert.equal(stateOf(86.1, b), 'critical');
  assert.equal(stateOf(99, null), 'ok');
  assert.equal(stateOf(null, b), 'ok');
});

test('a status always comes with a word', () => {
  assert.ok(STATE_WORD.warning && STATE_WORD.critical);
});

test('arcs are clamped and an empty one is still a valid path', () => {
  assert.match(arcPath(0, 0, 38), /^M[\d. -]+$/);
  assert.equal(arcPath(0, 1.5, 38), arcPath(0, 1, 38));
  // 240 degrees is more than half a turn; 0.25 of it is not
  assert.match(arcPath(0, 1, 38), / 0 1 1 /);
  assert.match(arcPath(0, 0.25, 38), / 0 0 1 /);
});

test('dials wrap onto a second line when that makes them bigger', () => {
  // three in a wide, short box: one line
  assert.equal(dialFit(3, 600, 120, 6, 14).perLine, 3);
  // eight in a narrow, tall box: more than one line
  const tall = dialFit(8, 300, 400, 6, 14);
  assert.ok(tall.perLine < 8, JSON.stringify(tall));
  assert.ok(tall.size > dialFit(8, 300, 400, 6, 14).size - 1);
  // nothing measured yet
  assert.equal(dialFit(3, 0, 120, 6, 14).size, 0);
});

test('readings: whole numbers, one decimal for temperatures and rates', () => {
  assert.equal(formatReading(spec('gpu_utilization'), 41.6), '42');
  assert.equal(formatReading(spec('gpu_temperature'), 84.46), '84.5');
  assert.equal(formatReading(spec('tokens_per_second'), 31.04), '31.0');
  assert.equal(formatReading(spec('gpu_power'), null), '—');
});


/* --- Network (BA3), with sparketa's real interfaces, read 2026-10-15 --- */

const iface = (name, speed, rxBytes, txBytes, extra = {}) => ({
  name, up: true, monitored: true, wireless: false, driver: 'mlx5_core', bus: 'pci',
  speed_mbps: speed, rx_bytes_per_sec: rxBytes, tx_bytes_per_sec: txBytes, ...extra,
});
const sparketa = {
  node_id: 'sparketa', up: true,
  rdma: ['enP2p1s0f0np0', 'enP2p1s0f1np1', 'enp1s0f0np0', 'enp1s0f1np1'].map((i, k) => ({ device: `rocep${k}`, port: 1, interface: i })),
  network: [
    iface('enP2p1s0f0np0', 200000, 1e9, 1e9),
    iface('enP2p1s0f1np1', 100000, 0, 0),
    iface('enp1s0f0np0', 200000, 2e9, 0),
    iface('enp1s0f1np1', 100000, 0, 0),
    iface('enP7s7', 10000, 62e6, 1e6, { driver: 'r8127' }),
    iface('wlP9s9', null, 0, 0, { up: false, driver: 'mt7925e', wireless: true }),
  ],
};
const sparky = {
  node_id: 'sparky', up: true, rdma: [],
  network: [iface('enP7s7', 10000, 1e6, 1e6, { driver: 'r8127' })],
};

test('a group adds its interfaces, and its capacity over the same ones', () => {
  const [roce, mgmt] = linkLoads([sparky, sparketa], ['fabric', 'management']);
  assert.equal(roce.label, 'RoCE');
  // sparky has no RoCE interface, so no dial there
  assert.deepEqual(roce.loads.map((l) => l.node), ['sparketa']);
  const l = roce.loads[0];
  assert.equal(l.links, 4);
  assert.equal(l.rxBits, 24e9);
  assert.equal(l.capacityBits, 600e9);
  assert.deepEqual(mgmt.loads.map((x) => x.node), ['sparky', 'sparketa']);
});

test('hidden groups and down nodes are left out', () => {
  assert.deepEqual(linkLoads([sparketa], ['management']).map((g) => g.key), ['management']);
  assert.equal(linkLoads([{ ...sparketa, up: false }], ['fabric']).length, 0);
});

test('a link with no reported speed makes its group tiles', () => {
  const [wifi] = linkLoads([sparketa], ['wifi']);
  assert.equal(wifi.loads[0].capacityBits, null);
  assert.equal(linkItems(wifi, 'rx', () => 'c').tiles, true);
});

test('network dials read as a share of the negotiated rate', () => {
  const [roce] = linkLoads([sparketa], ['fabric']);
  const { items, tiles } = linkItems(roce, 'rx', () => 'c');
  assert.equal(tiles, false);
  assert.equal(items[0].fraction, 24e9 / 600e9);
  assert.equal(items[0].value, '24.00');
  assert.equal(items[0].unit, 'Gb/s in');
  assert.deepEqual(items[0].scale, ['0', '600G']);
  assert.equal(capacityLabel(10e9), '10G');
});

test('system items carry state, bands as fractions, and idle', () => {
  const hot = node({ gpu: { util_pct: 1, temp_c: 87, power_w: 1, clock_mhz: 1, clock_state: 'PASS' } });
  const [it] = systemItems(spec('gpu_temperature'), [hot], () => 'c');
  assert.equal(it.state, 'critical');
  assert.deepEqual(it.bands, [0.82, 0.86]);
  const idle = node({ runtimes: { llama_cpp: [], vllm: [], sglang: [], tensorfold: [] } });
  assert.equal(systemItems(spec('tokens_per_second'), [idle], () => 'c')[0].value, 'idle');
  // from Prometheus: a node it did not answer for reads "—", never zero
  assert.equal(systemItems(spec('cpu_clock'), [node()], () => 'c', {})[0].value, '—');
});

console.log(`${passed}/${passed + failed} passed`);
if (failed) process.exit(1);
