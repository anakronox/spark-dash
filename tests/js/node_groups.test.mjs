/* Settings → Nodes grouping (roadmap CT), executed. */
import assert from 'node:assert/strict';
import { clusterNames, groupNodes } from '../../frontend/src/lib/node-groups.ts';

let passed = 0, failed = 0;
const test = (name, fn) => { try { fn(); passed++; } catch (e) { failed++; console.error(`FAIL ${name}\n${e.message}\n`); } };

const n = (node_id, cluster) => ({ node_id, cluster });

test('members are pulled together, groups in order of first appearance', () => {
  const g = groupNodes([n('sparky', null), n('sparketa', 'danflashes'), n('box', null), n('sparkjr', 'danflashes')]);
  assert.deepEqual(g.map((x) => x.key), ['solo:0', 'danflashes', 'solo:2']);
  assert.deepEqual(g[1].members.map((m) => [m.node.node_id, m.index]), [['sparketa', 1], ['sparkjr', 3]]);
});

test('each standalone node is a group of its own', () => {
  const g = groupNodes([n('a', null), n('b', '')]);
  assert.equal(g.length, 2);
  assert.ok(g.every((x) => x.cluster === null && x.members.length === 1));
});

test('a name with stray spaces is the same cluster', () => {
  const g = groupNodes([n('a', 'pair'), n('b', ' pair ')]);
  assert.deepEqual(g.map((x) => [x.key, x.members.length]), [['pair', 2]]);
});

test('the index is the file position, whatever the grouping', () => {
  const g = groupNodes([n('a', 'x'), n('b', 'y'), n('c', 'x')]);
  assert.deepEqual(g.flatMap((x) => x.members.map((m) => m.index)), [0, 2, 1]);
});

test('cluster names are unique, trimmed and sorted', () => {
  assert.deepEqual(clusterNames([n('a', 'zed'), n('b', null), n('c', ' alpha'), n('d', 'zed')]), ['alpha', 'zed']);
});

console.log(`${passed}/${passed + failed} passed`);
if (failed) process.exit(1);
