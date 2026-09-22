/* The fleet updater's status wording, actually executed (roadmap AK).
 *
 * `lib/fleet.ts` is the fleet page's own `statusOf`/`lineOf` ported to
 * TypeScript, and it is nearly all branching: a Spark with nothing to install
 * can be up to date, held back, waiting on its board vendor, or waiting on a
 * vendor that HAS published something newer -- four different sentences from
 * the same counts. A regex over the source asserting the word "waiting"
 * appears would pass on an implementation that gets every one of them
 * backwards. Run from tests/test_fleet_js.py, the network_history runner.
 */

import assert from 'node:assert/strict';

import {
  clusterSummary,
  etaText,
  installable,
  lastRunOf,
  lineOf,
  needsPassword,
  nextCheckText,
  pinnedOf,
  runProgress,
  shortFirmware,
  statusOf,
  summaryOf,
} from '../../frontend/src/lib/fleet.ts';

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

/** A reachable, current Spark. Override what the case is about. */
const spark = (o = {}) => ({
  name: 'sparky',
  host: '192.168.50.61',
  reachable: true,
  release: {
    on: 'OTA2607', on_name: 'July 2026', on_date: '2026-07-01',
    latest: 'OTA2607', latest_name: 'July 2026', latest_date: '2026-07-01',
    available: false, description: '', release_notes_url: '', checks_passed: 9, checks_total: 9,
  },
  updates: { total: 0, security: 0, packages: [] },
  firmware_updates: [],
  software: { state: 'current' },
  firmware_gap: { state: 'current' },
  platform_firmware: { state: 'nvidia-release' },
  ...o,
});

const behind = (o = {}) =>
  spark({
    release: { ...spark().release, on: 'OTA2.2', on_name: 'March 2026', available: true },
    ...o,
  });

// ---- statusOf: the pill ------------------------------------------------------

test('a run outranks everything, and says which kind', () => {
  const run = { id: 'r', rehearsal: false, nodes: [] };
  assert.deepEqual(statusOf(spark({ run, reachable: false })), { tone: 'info', text: 'updating' });
  assert.deepEqual(statusOf(spark({ run: { ...run, rehearsal: true } })), { tone: 'info', text: 'rehearsing' });
});

test('never checked, checking, unreachable are three states', () => {
  assert.deepEqual(statusOf({ name: 'x', host: 'h', reachable: null }), { tone: '', text: 'not checked yet' });
  assert.deepEqual(statusOf({ name: 'x', host: 'h', reachable: null, checking: true }), { tone: '', text: 'checking…' });
  assert.deepEqual(statusOf({ name: 'x', host: 'h', reachable: false }), { tone: 'critical', text: 'unreachable' });
  // Checking a Spark that HAS been seen keeps its last verdict on the pill.
  assert.equal(statusOf(spark({ checking: true })).text, 'up to date with NVIDIA');
});

test('unreadable firmware is said before any verdict on the release', () => {
  assert.deepEqual(statusOf(behind({ firmware_readable: false })), { tone: 'warning', text: "can't check firmware" });
});

test('anything apt or fwupd can install is "update available"', () => {
  assert.equal(statusOf(behind({ updates: { total: 210, security: 133 } })).text, 'update available');
  assert.equal(statusOf(spark({ firmware_updates: [{ device: 'UEFI', now: '1', after: '2' }] })).text, 'update available');
  assert.equal(installable(spark({ updates: { total: 3, security: 0 } })), true);
  assert.equal(installable(spark({ reachable: false, updates: { total: 3, security: 0 } })), false);
});

test('the partner-board cases: waiting on the vendor is not neglect, a newer bundle is', () => {
  const pending = behind({
    firmware_gap: { state: 'pending-vendor', vendor: 'ASUS', outstanding: [{ label: 'SBIOS' }] },
    platform_firmware: { state: 'current', vendor: 'ASUS', installed: '1.05', newest: { bundle: '1.05' } },
  });
  assert.deepEqual(statusOf(pending), { tone: 'warning', text: 'waiting on ASUS firmware' });

  const vendorHasNewer = behind({
    firmware_gap: { state: 'pending-vendor', vendor: 'ASUS', outstanding: [{ label: 'SBIOS' }] },
    platform_firmware: { state: 'behind', vendor: 'ASUS', installed: '1.05', newest: { bundle: '1.07' } },
  });
  assert.deepEqual(statusOf(vendorHasNewer), { tone: 'warning', text: 'ASUS firmware 1.07 available' });

  const nvidia = behind({ firmware_gap: { state: 'pending-nvidia', outstanding: [] } });
  assert.deepEqual(statusOf(nvidia), { tone: 'warning', text: 'waiting on NVIDIA firmware' });
});

test('behind with nothing to install and no firmware excuse is held back, counted', () => {
  const held = behind({ software: { state: 'held', behind: [{ name: 'a' }, { name: 'b' }] } });
  assert.deepEqual(statusOf(held), { tone: 'warning', text: '2 packages held back' });
  assert.equal(statusOf(behind({ software: { state: 'held', behind: [{ name: 'a' }] } })).text, '1 package held back');
  assert.equal(statusOf(behind()).text, 'not on the latest');
});

test('current is good', () => {
  assert.deepEqual(statusOf(spark()), { tone: 'good', text: 'up to date with NVIDIA' });
});

// ---- lineOf: the sentence --------------------------------------------------

test('a run says what step the node is on, or that it is waiting', () => {
  const run = (me) => ({ id: 'r', rehearsal: false, nodes: [me] });
  assert.equal(lineOf(spark({ run: run({ name: 'sparky', status: 'running', step: 'install' }) })), 'installing');
  assert.equal(lineOf(spark({ run: run({ name: 'sparky', status: 'queued', step: null }) })), 'waiting its turn');
  assert.equal(lineOf(spark({ run: run({ name: 'other', status: 'running', step: 'check' }) })), '…');
});

test('unreachable carries the error, clipped', () => {
  assert.equal(lineOf({ name: 'x', host: 'h', reachable: false }), 'could not connect');
  assert.equal(lineOf({ name: 'x', host: 'h', reachable: false, error: 'x'.repeat(100) }), 'could not connect · ' + 'x'.repeat(80));
});

test('a release with updates names the release and counts them', () => {
  assert.equal(lineOf(behind({ updates: { total: 210, security: 133 } })), 'NVIDIA July 2026 release · 210 updates, 133 security');
  // Counts not in yet: only firmware makes this the "release" sentence; with
  // nothing to install at all it is the held-back one, as on the fleet page.
  assert.equal(
    lineOf(behind({ updates: { total: null, security: null }, firmware_updates: [{ device: 'UEFI', now: '1', after: '2' }] })),
    'NVIDIA July 2026 release · counting…',
  );
});

test('current with routine updates is Ubuntu, not NVIDIA', () => {
  assert.equal(lineOf(spark({ updates: { total: 12, security: 3 } })), '12 routine Ubuntu updates, 3 security');
  assert.equal(lineOf(spark()), 'no updates');
});

test('the vendor sentence changes with what the vendor has published', () => {
  const base = {
    firmware_gap: { state: 'pending-vendor', vendor: 'ASUS' },
    software: { state: 'current' },
  };
  const current = behind({ ...base, platform_firmware: { state: 'current', vendor: 'ASUS', installed: '1.05', newest: { bundle: '1.05' } } });
  assert.equal(lineOf(current), "software current for July 2026 · ASUS firmware 1.05 is the vendor's newest · NVIDIA July 2026 firmware pending from ASUS");
  const newer = behind({ ...base, platform_firmware: { state: 'behind', vendor: 'ASUS', installed: '1.05', newest: { bundle: '1.07' } } });
  assert.equal(lineOf(newer), 'software current for July 2026 · ASUS firmware 1.05, vendor has 1.07 — apply it their way · NVIDIA July 2026 firmware pending from ASUS');
  const noTable = behind({ ...base, software: { state: 'installable' } });
  assert.equal(lineOf(noTable), 'on March 2026 · NVIDIA July 2026 firmware pending from ASUS · nothing to install');
});

test('a pin is not "no updates", and is not the scorer\'s "held back"', () => {
  // AL6.3. The whole point: apt leaves a held package out of the plan, so the
  // count is honestly zero and the sentence would still be a lie.
  const pinned = spark({
    updates: { total: 0, security: 0, packages: [], held: ['linux-nvidia-hwe-24.04'], kept_back: ['linux-nvidia-hwe-24.04'] },
  });
  assert.equal(lineOf(pinned), 'no updates it may install · 1 package pinned here');
  // Pinning a kernel pins the driver stack behind it; that second number is
  // the surprise, so it is said only when it is bigger.
  const withDeps = spark({
    updates: {
      total: 0, security: 0, packages: [],
      held: ['linux-nvidia-hwe-24.04', 'linux-image-nvidia-hwe-24.04'],
      kept_back: ['linux-nvidia-hwe-24.04', 'linux-image-nvidia-hwe-24.04', 'libnvidia-compute-580'],
    },
  });
  assert.equal(lineOf(withDeps), 'no updates it may install · 2 packages pinned here, 3 kept back');
  // A pin rides along with a real count rather than replacing it.
  assert.equal(
    lineOf(spark({ updates: { total: 12, security: 3, held: ['curl'], kept_back: ['curl'] } })),
    '12 routine Ubuntu updates, 3 security · 1 package pinned here',
  );
  // And it must not be confused with the scorer's unrelated "held back".
  assert.equal(pinnedOf(spark()), null);
  assert.equal(pinnedOf(behind({ software: { state: 'held', behind: [{ name: 'nvidia-driver' }] } })), null);
});

test('held back names the packages, or admits it cannot', () => {
  assert.equal(lineOf(behind({ software: { state: 'held', behind: [{ name: 'nvidia-driver' }] } })), 'on March 2026 · held back: nvidia-driver · nothing to install');
  assert.equal(lineOf(behind({ software: { state: 'held' } })), 'on March 2026 · held back: checks failing · nothing to install');
});

test('unreadable firmware points at the guide and still counts packages', () => {
  assert.equal(
    lineOf(behind({ firmware_readable: false, updates: { total: 5, security: 1 } })),
    'firmware needs the two read-only sudo rules (see the guide) · 5 package updates, 1 security',
  );
});

// ---- the run -----------------------------------------------------------------

test('one bar across four steps, and the install phase moves inside its band', () => {
  const at = (step, extra = {}) => runProgress({ name: 'n', status: 'running', step, steps: {}, ...extra });
  assert.equal(runProgress({ name: 'n', status: 'queued', step: null, steps: {} }).pct, 0);
  assert.equal(at('check').pct, 2);
  assert.equal(at('install').pct, 3);
  assert.equal(at('install', { steps: { install: { progress: 0.5, phase: 'apt-upgrade' } } }).pct, 42);
  assert.equal(at('install', { steps: { install: { status: 'ok' } } }).pct, 80);
  assert.equal(at('restart').pct, 88);
  assert.equal(at('verify').pct, 97);
  assert.equal(at('verify').done, true);
  assert.equal(at('restart').done, false);
});

test('the step text is a phase during install and a plain verb otherwise', () => {
  const at = (step, steps = {}) => runProgress({ name: 'n', status: 'running', step, steps });
  assert.equal(at('install', { install: { phase: 'apt-upgrade' } }).stepText, 'installing packages');
  assert.equal(at('install', { install: { phase: 'lock' } }).stepText, 'waiting for other installs to finish');
  assert.equal(at('install').stepText, 'starting');
  assert.equal(at('restart', { restart: { detail: 'pinging' } }).stepText, 'restarting · pinging');
  assert.equal(at('restart').stepText, 'restarting · waiting for it to come back');
  assert.equal(at('check').stepNo, 1);
  assert.equal(at('verify').stepNo, 4);
  assert.equal(runProgress(undefined), null);
});

test('the eta rounds like a person would', () => {
  assert.equal(etaText(null), '');
  assert.equal(etaText(30), 'under a minute left');
  assert.equal(etaText(150), 'about 3 min left');
  assert.equal(etaText(1234), 'about 20 min left');
});

test('the last-run line: passed, rehearsed, failed, back on a new boot, stopped', () => {
  const now = Date.parse('2026-09-16T12:00:00Z');
  const done = (me, o = {}) => ({
    id: 'r1', started_at: '2026-09-16T11:00:00Z', finished_at: '2026-09-16T11:30:00Z',
    rehearsal: false, status: 'ok', message: '', nodes: [{ name: 'sparky', steps: {}, ...me }], ...o,
  });
  const ok = lastRunOf(spark({ last_run: done({ status: 'ok', changed: { release: ['March 2026', 'July 2026'], updates: [210, 0] } }) }), now);
  assert.equal(ok.kind, 'ok');
  assert.match(ok.text, /March 2026 → July 2026/);
  assert.match(ok.text, /210 → 0 package updates/);

  const reh = lastRunOf(spark({ last_run: done({ status: 'ok' }, { rehearsal: true }) }), now);
  assert.equal(reh.kind, 'rehearsal');

  const failed = lastRunOf(spark({ last_run: done({ status: 'failed', step: 'install', steps: { install: { error: 'apt rc=11' } } }, { status: 'failed' }) }), now);
  assert.equal(failed.kind, 'failed');
  assert.match(failed.text, /stopped at install/);
  assert.match(failed.text, /apt rc=11/);

  // The wait for the restart timed out, but the boot id moved: verify, don't panic.
  const back = lastRunOf(
    spark({ boot_id: 'new', last_run: done({ status: 'failed', step: 'restart', before: { boot_id: 'old' } }, { status: 'failed' }) }),
    now,
  );
  assert.equal(back.kind, 'back');
  const notBack = lastRunOf(
    spark({ boot_id: 'old', last_run: done({ status: 'failed', step: 'restart', before: { boot_id: 'old' } }, { status: 'failed', message: 'timed out' }) }),
    now,
  );
  assert.equal(notBack.kind, 'failed');

  const stopped = lastRunOf(spark({ last_run: done({ status: 'stopped', step: 'check' }, { status: 'stopped' }) }), now);
  assert.equal(stopped.kind, 'stopped');

  // Old news is not shown, and nothing is shown under a live run.
  assert.equal(lastRunOf(spark({ last_run: done({ status: 'ok' }) }), now + 4 * 86400e3), null);
  assert.equal(lastRunOf(spark({ last_run: done({ status: 'ok' }), run: { id: 'r2', rehearsal: false, nodes: [] } }), now), null);
});

// ---- the header ----------------------------------------------------------------

test('the summary counts what needs doing and says who is waiting on a vendor', () => {
  const f = {
    nodes: [
      behind({ name: 'sparky', updates: { total: 210, security: 133 } }),
      behind({ name: 'sparkjr', firmware_gap: { state: 'pending-vendor', vendor: 'ASUS' } }),
      spark({ name: 'sparketa' }),
    ],
    clusters: [], last_sweep: null, next_sweep: null,
  };
  assert.equal(summaryOf(f), '3 Sparks · 1 has updates · 1 waiting on vendor firmware · last checked —');
  assert.equal(summaryOf({ nodes: [] }), 'no Sparks yet');
});

test('the countdown', () => {
  const now = Date.parse('2026-09-16T12:00:00Z');
  assert.equal(nextCheckText({ nodes: [], next_sweep: '2026-09-16T12:12:00Z' }, now), 'Next check in: 12 minutes');
  assert.equal(nextCheckText({ nodes: [], next_sweep: '2026-09-16T12:01:00Z' }, now), 'Next check in: 1 minute');
  assert.equal(nextCheckText({ nodes: [], next_sweep: '2026-09-16T11:59:00Z' }, now), 'Next check: any moment');
  assert.equal(nextCheckText({ nodes: [{ checking: true }], next_sweep: '2026-09-16T12:12:00Z' }, now), 'Checking now…');
  assert.equal(nextCheckText(null, now), '');
});

test('odds and ends', () => {
  assert.equal(needsPassword({ sudo_passwordless: true }), false);
  assert.equal(needsPassword({ sudo_passwordless: false }), true);
  assert.equal(needsPassword(undefined), true, 'unknown means ask');
  assert.equal(shortFirmware('FW1: 28.43.1014 FW2: x'), '28.43.1014');
  assert.equal(shortFirmware('SBP:R:1.2.3'), '1.2.3');
  assert.equal(shortFirmware(null), 'not reported');
  assert.equal(clusterSummary({ members: ['a', 'b'], links: [{ speed_mbps: 200000 }, { speed_mbps: 200000 }] }), '2 direct links · 200 GbE');
  assert.equal(clusterSummary({ members: ['a', 'b'], links: [] }), 'linked');
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
