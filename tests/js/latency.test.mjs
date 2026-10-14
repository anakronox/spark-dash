/* The header's time-to-first-token formatting (roadmap AT), executed. */
import assert from 'node:assert/strict';
import { latencyTitle, seconds } from '../../frontend/src/lib/latency.ts';

let passed = 0, failed = 0;
const test = (name, fn) => { try { fn(); passed++; } catch (e) { failed++; console.error(`FAIL ${name}\n${e.message}\n`); } };

test('milliseconds below a second, one decimal to ten, whole seconds past', () => {
  assert.deepEqual([0.085, 0.85, 1.54, 12.4].map(seconds), ['85ms', '850ms', '1.5s', '12s']);
});

test('no figure is a dash, never NaN', () => {
  assert.deepEqual([null, undefined, NaN, Infinity].map(seconds), ['—', '—', '—', '—']);
});

const glm = { model: 'GLM-5.3-Flash-EXL3', node: 'sparketa', runtime: 'tensorfold', source: 'engine', p50_s: 1.54, p95_s: 2.48, per_min: 12, accept: 0.71 };
const qwen = { model: 'sparky-prod/qwen36-35b', node: null, runtime: null, source: 'gateway', p50_s: 0.4, p95_s: 0.9, per_min: 3, accept: null };

test('the hover lists every model with where it ran and its acceptance', () => {
  const t = latencyTitle({ window: '15m', busiest: glm, models: [glm, qwen] }).split('\n');
  assert.equal(t[0], 'Time to first token, last 15m, the busiest model shown.');
  assert.equal(t[1], 'GLM-5.3-Flash-EXL3 (sparketa · tensorfold): p50 1.5s, p95 2.5s, 12 req/min, draft acceptance 71%');
  assert.equal(t[2], 'sparky-prod/qwen36-35b (via the gateway): p50 400ms, p95 900ms, 3 req/min');
});

test('an idle window says so', () => {
  const idle = { ...glm, p50_s: null, p95_s: null, per_min: 0, accept: null };
  assert.match(latencyTitle({ window: '15m', busiest: null, models: [idle] }), /No requests in the window\.$/);
});

console.log(`${passed}/${passed + failed} passed`);
if (failed) process.exit(1);
