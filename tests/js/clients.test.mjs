/* Client stats: Settings' states and wording, actually executed (roadmap AM5).
 *
 * `lib/clients.ts` decides which of five branches Settings renders and what
 * each says. The order matters -- a deployment with no cluster file must be
 * told that before anything about addresses, and a broken block before
 * "needs a gateway" -- and a source guard could not check an order. Run from
 * tests/test_clients_js.py, the fleet runner's twin.
 */

import assert from 'node:assert/strict';

import { probeLine, sameAddress, stateLine, viewOf } from '../../frontend/src/lib/clients.ts';

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

/** Configured, on and scraped. Override what the case is about. */
const status = (o = {}) => ({
  capability: true,
  enabled: true,
  configured: true,
  url: 'http://litellm.invalid:4000',
  error: null,
  cluster_file: true,
  state: 'ok',
  ...o,
});

test('views, in the order that matters', () => {
  assert.equal(viewOf(null), 'absent', 'not loaded yet reads as not set up');
  assert.equal(viewOf(status()), 'on');
  assert.equal(viewOf(status({ enabled: false, configured: false })), 'off');
  assert.equal(viewOf(status({ capability: false, enabled: false, url: null })), 'absent');
  assert.equal(viewOf(status({ capability: false, error: 'bad url' })), 'invalid');
  assert.equal(
    viewOf(status({ cluster_file: false, capability: false, error: 'bad url' })),
    'no-cluster-file',
    'no cluster file outranks everything: nothing else can be fixed first',
  );
});

test('state lines while on', () => {
  assert.deepEqual(stateLine(status()), { tone: '', text: 'Prometheus is reading the gateway.' });
  const stale = stateLine(status({ state: 'not scraped' }));
  assert.equal(stale.tone, 'warning');
  assert.match(stale.text, /30 seconds/);
  assert.match(stale.text, /Test/);
  assert.equal(stateLine(status({ state: 'unknown' })).tone, 'warning');
  assert.equal(stateLine(status({ state: 'something new' })).text, 'something new', 'unknown states show verbatim');
});

test('probe lines carry the backend sentence', () => {
  const ok = { url: 'x', reachable: true, metrics: 'ok', detail: 'Reachable, and its metrics are readable.' };
  assert.deepEqual(probeLine(ok), { tone: '', text: ok.detail });
  for (const metrics of ['needs a key', 'missing', 'error', 'not checked']) {
    assert.equal(probeLine({ ...ok, metrics, detail: 'd' }).tone, 'warning', metrics);
  }
});

test('the pasted client URL is not a change', () => {
  const saved = 'http://litellm.invalid:4000';
  assert.ok(sameAddress('http://litellm.invalid:4000/v1', saved));
  assert.ok(sameAddress(' http://litellm.invalid:4000/ ', saved));
  assert.ok(sameAddress('http://litellm.invalid:4000/v1/', saved));
  assert.ok(!sameAddress('http://litellm.invalid:4001', saved));
  assert.ok(sameAddress('', null));
  assert.ok(!sameAddress('http://litellm.invalid:4000', null));
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
