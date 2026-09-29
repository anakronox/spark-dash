/* Client stats: Settings' states and wording, actually executed (roadmap AM5).
 *
 * `lib/clients.ts` decides which of five branches Settings renders and what
 * each says. The order matters -- a deployment with no cluster file must be
 * told that before anything about addresses, and a broken block before
 * "needs a gateway" -- and a source guard could not check an order. Run from
 * tests/test_clients_js.py, the fleet runner's twin.
 */

import assert from 'node:assert/strict';

import {
  clientTitle,
  countingNote,
  engineText,
  failedTitle,
  harnessTitle,
  modelText,
  probeLine,
  rowKey,
  sameAddress,
  stateLine,
  summaryText,
  viewOf,
} from '../../frontend/src/lib/clients.ts';

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

/** A row as GET /api/clients returns it. Override what the case is about. */
const row = (o = {}) => ({
  harness: { name: 'OpenAI SDK (Python)', kind: 'sdk' },
  user_agent: 'OpenAI/Python 2.24.0',
  client: { ip: '10.0.0.99', node: null, name: 'agents', fqdn: 'agents.lan.invalid' },
  model: 'flash/glm',
  rejected: false,
  engine: { server: '10.0.0.2:8003', node: 'sparketa', runtime: 'vllm' },
  requests: 212,
  failed: 0,
  statuses: {},
  routes: [],
  per_min: 3,
  active: true,
  ...o,
});

test('engine: node and runtime when monitored, the address when not, a dash when none', () => {
  assert.equal(engineText(row()), 'sparketa · vllm');
  assert.equal(engineText(row({ engine: { server: '10.9.9.9:9000', node: null, runtime: null } })), '10.9.9.9:9000');
  assert.equal(engineText(row({ engine: null })), '—');
});

test('a rejected request says so instead of naming a model', () => {
  assert.equal(modelText(row()), 'flash/glm');
  assert.equal(modelText(row({ rejected: true, model: null })), 'no such model');
});

test('harness tooltips explain the name', () => {
  assert.match(harnessTitle(row()), /^OpenAI\/Python 2\.24\.0\. A library's default/);
  assert.match(harnessTitle(row({ harness: { name: 'Anthropic-API client', kind: 'unattributed' }, user_agent: 'None' })), /drops the User-Agent/);
  assert.match(harnessTitle(row({ harness: { name: 'opencode', kind: 'unknown' }, user_agent: 'opencode/1.2' })), /^opencode\/1\.2\. Not a harness/);
  assert.equal(harnessTitle(row({ harness: { name: 'Claude Code', kind: 'harness' }, user_agent: 'claude-cli/2.1.0' })), 'claude-cli/2.1.0');
});

test('client tooltips: node, reverse DNS, or the lack of it, then the address', () => {
  assert.equal(clientTitle(row()), 'agents.lan.invalid · 10.0.0.99');
  assert.equal(clientTitle(row({ client: { ip: '10.0.0.2', node: 'sparketa', name: 'sparketa', fqdn: null } })), 'node sparketa · 10.0.0.2');
  assert.equal(clientTitle(row({ client: { ip: '10.0.0.77', node: null, name: '10.0.0.77', fqdn: null } })), 'no reverse DNS · 10.0.0.77');
});

test('failures by status, most common first', () => {
  assert.equal(failedTitle(row({ statuses: { 404: 1, 500: 3 } })), '3 × 500, 1 × 404');
  assert.equal(failedTitle(row()), '');
});

test('summary counts machines, not rows', () => {
  assert.equal(summaryText([]), '');
  assert.equal(
    summaryText([row(), row({ model: 'other/m', requests: 8 }), row({ client: { ip: '10.0.0.50', node: null, name: 'laptop', fqdn: null }, requests: 1 })]),
    '2 clients · 221 requests',
  );
  assert.equal(summaryText([row({ requests: 1 })]), '1 client · 1 request');
});

test('counting since, only when the scrape began inside the window', () => {
  assert.equal(countingNote(null), '');
  assert.match(countingNote(1790685442, 'en-GB'), /^counting since \d\d:\d\d$/);
});

test('row keys are stable and distinct', () => {
  assert.equal(rowKey(row()), rowKey(row({ requests: 5 })), 'counts are not identity');
  assert.notEqual(rowKey(row()), rowKey(row({ model: 'other/m' })));
  assert.notEqual(rowKey(row({ rejected: true, model: null })), rowKey(row({ model: null })));
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
