/** The fleet updater's data, and what to say about it (roadmap AK).
 *
 * TYPES MIRROR spark-fleet-updates' JSON. That service is the source of
 * truth for what a Spark is on and what it could be on; the dashboard is a
 * window onto it through the backend proxy, and re-describes nothing. The
 * shapes below are its posture record (`spark_fleet/posture.py`), its run
 * state (`spark_fleet/executor.py`) and its `/api/fleet` envelope
 * (`spark_fleet/service.py`), with only the fields the panel reads.
 *
 * THE WORDING IS PORTED, NOT INVENTED. `statusOf` and `lineOf` are the fleet
 * page's own functions (`spark_fleet/web/index.html`), rewritten in TypeScript
 * with the same branches in the same order. `pinnedOf` is the exception and
 * is new here (AL6.3); it is ported the other way, into that page. The hard-won part is the partner
 * board case: a Spark with nothing to install that is still not on NVIDIA's
 * latest, because its vendor has not published the firmware, must read as
 * "waiting on ASUS" and never as neglected -- and one whose vendor HAS
 * published a newer bundle must read as behind. Three lines that must not
 * collapse into one. Keep this file in step with that one when either moves.
 *
 * Plain TypeScript with no runes and no DOM, so it runs under node in
 * tests/js/fleet.test.mjs -- the branching is exactly what a source-level
 * guard cannot check.
 */

export interface FleetPackage {
  name: string;
  from: string | null;
  to: string;
  new: boolean;
  security: boolean;
  source: string;
}

export interface FirmwareUpdate {
  device: string;
  now: string;
  after: string;
  size?: number | null;
  summary?: string;
  vendor?: string;
  needs_reboot?: boolean;
}

/** A firmware check the newest release wants and the board does not meet. */
export interface FirmwareGapItem {
  name: string;
  label: string;
  installed: string | null;
  target: string;
}

export interface FleetRelease {
  on: string;
  on_name: string;
  on_date: string;
  latest: string;
  latest_name: string;
  latest_date: string;
  available: boolean;
  description: string;
  release_notes_url: string;
  checks_passed: number;
  checks_total: number;
}

export interface FleetSoftware {
  state: 'current' | 'installable' | 'held' | string;
  behind?: { name: string; installed: string | null; target: string }[];
  missing?: { name: string }[];
}

export interface PlatformFirmware {
  state: 'current' | 'behind' | 'newer' | 'unknown' | 'nvidia-release' | string;
  vendor?: string;
  model?: string;
  installed?: string | null;
  bios_version?: string;
  bios_date?: string;
  newest?: { bundle: string; published: string; checked: string } | null;
}

export interface FirmwareGap {
  state: 'current' | 'installable' | 'pending-vendor' | 'pending-nvidia' | string;
  vendor?: string | null;
  outstanding?: FirmwareGapItem[];
  not_reported?: FirmwareGapItem[];
}

export type RunStep = 'check' | 'install' | 'restart' | 'verify';

export interface RunStepState {
  status?: string;
  error?: string;
  detail?: string;
  progress?: number | null;
  phase?: string;
}

export interface RunNode {
  name: string;
  status: 'queued' | 'running' | 'ok' | 'failed' | string;
  step: RunStep | null;
  steps: Partial<Record<RunStep, RunStepState>>;
  progress: number | null;
  before: { boot_id?: string; release?: string } | null;
  after: { boot_id?: string; release?: string } | null;
  changed: { release?: [string, string]; updates?: [number, number] } | null;
  eta_seconds: number | null;
  release_note?: string;
}

export interface FleetRun {
  id: string;
  started_at: string;
  finished_at: string | null;
  rehearsal: boolean;
  status: 'running' | 'ok' | 'failed' | 'stopped' | string;
  message: string;
  nodes: RunNode[];
}

export interface FleetNode {
  name: string;
  host: string;
  /** null: never checked. false: the last check could not connect. */
  reachable: boolean | null;
  collected_at?: string;
  error?: string;
  hostname?: string;
  board?: string;
  board_kind?: string;
  boot_id?: string;
  release?: FleetRelease;
  software?: FleetSoftware;
  platform_firmware?: PlatformFirmware;
  firmware_gap?: FirmwareGap;
  updates?: {
    total: number | null;
    security: number | null;
    as_of?: string | null;
    counts?: { upgraded: number; new: number; removed: number };
    packages?: FleetPackage[];
    /* AL6.3. `held` is what a person pinned with `apt-mark hold`; `kept_back`
     * is what apt declined to install because of it, which is the held set
     * plus everything depending on it. A held package NEVER appears in
     * `packages` -- apt leaves it out of the plan entirely -- so without
     * these two a pinned Spark reads as "no updates". */
    held?: string[];
    kept_back?: string[];
  };
  firmware_updates?: FirmwareUpdate[];
  cx7?: { state: 'ok' | 'behind' | 'not-verifiable'; version?: string; reason?: string };
  reboot?: { required: boolean; packages?: string[] };
  dashboard_auto_update?: boolean | null;
  sudo_passwordless?: boolean | null;
  firmware_readable?: boolean;
  /* Added by the fleet service's fleet() view, not the posture record. */
  checking?: boolean;
  pair?: string[];
  run?: FleetRun | null;
  last_run?: FleetRun | null;
}

export interface FleetLink {
  a: string;
  a_port: string;
  b: string;
  b_port: string;
  speed_mbps: number;
  via: string;
}

export interface FleetCluster {
  name?: string;
  members: string[];
  links: FleetLink[];
}

export interface Fleet {
  nodes: FleetNode[];
  clusters: FleetCluster[];
  latest: { name: string; external_name: string; date: string };
  last_sweep: string | null;
  next_sweep: string | null;
  interval_min: number;
  ssh_user: string | null;
  now: string;
}

/** The backend's envelope around the fleet service's answer. */
export interface FleetEnvelope {
  configured: boolean;
  available: boolean;
  public_url: string | null;
  fleet: Fleet | null;
}

/** Tones the pill can take. `info` is a run in progress -- neither good nor
 *  bad, just happening -- and the empty tone is "not known yet". Neither maps
 *  onto a HealthState, which is why this is not StatusPill. */
export type FleetTone = 'good' | 'warning' | 'critical' | 'info' | '';

export interface FleetStatus {
  tone: FleetTone;
  text: string;
}

/** Something apt or fwupd could install right now. */
export function installable(n: FleetNode): boolean {
  return Boolean(n.reachable && (n.updates?.total || (n.firmware_updates ?? []).length));
}

/** The fleet service asks for a sudo password unless the Spark grants this
 *  user NOPASSWD: ALL; "unknown" is treated as "ask". */
export function needsPassword(n: FleetNode | undefined): boolean {
  return n?.sudo_passwordless !== true;
}

/** A finished run worth still showing under the row: three days, as on the
 *  fleet page. */
export function recent(r: FleetRun, now: number = Date.now()): boolean {
  return Boolean(r.finished_at) && now - Date.parse(r.finished_at as string) < 86400e3 * 3;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`;

export function statusOf(n: FleetNode): FleetStatus {
  if (n.run) return { tone: 'info', text: n.run.rehearsal ? 'rehearsing' : 'updating' };
  if (n.checking && n.reachable == null) return { tone: '', text: 'checking…' };
  if (n.reachable === false) return { tone: 'critical', text: 'unreachable' };
  if (n.reachable == null) return { tone: '', text: 'not checked yet' };
  if (n.firmware_readable === false) return { tone: 'warning', text: "can't check firmware" };
  const fg = n.firmware_gap;
  const pf = n.platform_firmware;
  if (installable(n)) return { tone: 'warning', text: 'update available' };
  if (n.release?.available) {
    // Nothing to install, yet not on the latest: say WHO is holding it up,
    // so a partner board that is as current as its vendor allows is not read
    // as neglected.
    if (fg?.state === 'pending-vendor' && pf?.state === 'behind' && pf.newest) {
      return { tone: 'warning', text: `${pf.vendor} firmware ${pf.newest.bundle} available` };
    }
    if (fg?.state === 'pending-vendor') {
      return { tone: 'warning', text: `waiting on ${fg.vendor} firmware` };
    }
    if (fg?.state === 'pending-nvidia') {
      return { tone: 'warning', text: 'waiting on NVIDIA firmware' };
    }
    const held = (n.software?.behind ?? []).length;
    return {
      tone: 'warning',
      text: held ? `${plural(held, 'package')} held back` : 'not on the latest',
    };
  }
  return { tone: 'good', text: 'up to date with NVIDIA' };
}

/** What a node is doing right now inside its run, in the words the fleet
 *  page uses. */
export function runLabel(n: FleetNode): string {
  const me = n.run?.nodes.find((x) => x.name === n.name);
  const labels: Record<string, string> = {
    check: 'checking it is safe',
    install: 'installing',
    restart: 'restarting',
    verify: 'verifying',
  };
  return (me?.step && labels[me.step]) || (me?.status === 'queued' ? 'waiting its turn' : '…');
}

/** AL6.3: what a person pinned on this Spark by hand, if anything.
 *
 * Deliberately NOT called "held": `lineOf` already says "held back" for a
 * release the scorer finds incomplete, and the two are unrelated. A pin is
 * someone's decision; "held back" is NVIDIA's release not fitting. Saying
 * "pinned" keeps them apart on a row where both can appear at once.
 */
export function pinnedOf(n: FleetNode): { count: number; kept: number; text: string } | null {
  const held = n.updates?.held ?? [];
  if (!held.length) return null;
  const kept = (n.updates?.kept_back ?? []).length;
  const pkgs = `${held.length} package${held.length === 1 ? '' : 's'} pinned here`;
  // The second number is the one that surprises: pinning a kernel pins the
  // driver stack that depends on it. Only worth saying when it is bigger.
  return { count: held.length, kept, text: kept > held.length ? `${pkgs}, ${kept} kept back` : pkgs };
}

export function lineOf(n: FleetNode): string {
  if (n.run) return runLabel(n);
  if (n.reachable === false) {
    return 'could not connect' + (n.error ? ' · ' + n.error.slice(0, 80) : '');
  }
  if (!n.release) return '';
  const u = n.updates ?? { total: null, security: null };
  if (n.firmware_readable === false) {
    const tail = u.total != null ? `${u.total} package updates, ${u.security} security` : '';
    return `firmware needs the two read-only sudo rules (see the guide) · ${tail}`;
  }
  const cnt = u.total != null ? `${u.total} updates, ${u.security} security` : 'counting…';
  if (n.release.available && !u.total && !(n.firmware_updates ?? []).length) {
    const fg = n.firmware_gap ?? { state: 'current' };
    const pf = n.platform_firmware ?? { state: 'unknown' };
    const sw = n.software ?? { state: 'unknown' };
    const swTxt =
      sw.state === 'current'
        ? `software current for ${n.release.latest_name}`
        : `on ${n.release.on_name}`;
    if (fg.state === 'pending-vendor' && pf.newest) {
      const vend =
        pf.state === 'current'
          ? `${pf.vendor} firmware ${pf.installed} is the vendor's newest`
          : pf.state === 'behind'
            ? `${pf.vendor} firmware ${pf.installed}, vendor has ${pf.newest.bundle} — apply it their way`
            : `${pf.vendor} firmware ${pf.installed || 'unknown'}`;
      return `${swTxt} · ${vend} · NVIDIA ${n.release.latest_name} firmware pending from ${pf.vendor}`;
    }
    if (fg.state === 'pending-vendor') {
      return `${swTxt} · NVIDIA ${n.release.latest_name} firmware pending from ${fg.vendor || 'the board vendor'} · nothing to install`;
    }
    if (fg.state === 'pending-nvidia') {
      return `${swTxt} · NVIDIA ${n.release.latest_name} firmware not offered to this board yet · nothing to install`;
    }
    const held = (sw.behind ?? []).map((f) => f.name).join(', ') || 'checks failing';
    return `${swTxt} · held back: ${held} · nothing to install`;
  }
  const pin = pinnedOf(n);
  const pinned = pin ? ` · ${pin.text}` : '';
  if (n.release.available) return `NVIDIA ${n.release.latest_name} release · ${cnt}${pinned}`;
  if (u.total) return `${u.total} routine Ubuntu updates, ${u.security} security${pinned}`;
  // "no updates" on a pinned Spark is the sentence this item exists to stop:
  // there may be plenty, and none of them allowed.
  return pin ? `no updates it may install · ${pin.text}` : 'no updates';
}

export const RUN_STEPS: RunStep[] = ['check', 'install', 'restart', 'verify'];

export interface RunProgress {
  /** 0–100 across the whole update: check 0–3, install 3–80, restart 80–95,
   *  verify 95–100 -- one bar, not four. */
  pct: number;
  /** 1-based step number, 0 while queued. */
  stepNo: number;
  stepText: string;
  /** The install phase's own detail line, when in that step. */
  detail: string;
  eta: string;
  /** Verify is the last step and the bar turns good for it. */
  done: boolean;
}

const PHASES: Record<string, string> = {
  lock: 'waiting for other installs to finish',
  'apt-update': 'refreshing the package list',
  'apt-upgrade': 'installing packages',
  'fw-refresh': 'checking for firmware',
  'fw-upgrade': 'installing firmware',
  done: 'packages and firmware installed',
};

export function etaText(seconds: number | null | undefined): string {
  if (seconds == null) return '';
  if (seconds < 60) return 'under a minute left';
  const mins = seconds < 600 ? Math.max(1, Math.round(seconds / 60)) : Math.round(seconds / 300) * 5;
  return `about ${mins} min left`;
}

export function runProgress(me: RunNode | undefined): RunProgress | null {
  if (!me) return null;
  const inst = me.steps?.install;
  const instFrac = inst?.progress ?? (inst?.status === 'ok' ? 1 : 0);
  const pct =
    me.status === 'queued'
      ? 0
      : me.step === 'check'
        ? 2
        : me.step === 'install'
          ? Math.round(3 + 77 * instFrac)
          : me.step === 'restart'
            ? 88
            : me.step === 'verify'
              ? 97
              : 100;
  const phaseTxt = (inst?.phase && PHASES[inst.phase]) || 'starting';
  const stepTexts: Record<RunStep, string> = {
    check: 'checking it is safe to update',
    install: phaseTxt,
    restart: 'restarting · ' + (me.steps?.restart?.detail || 'waiting for it to come back'),
    verify: 'verifying it is on the new release',
  };
  const stepText =
    (me.step && stepTexts[me.step]) || (me.status === 'queued' ? 'waiting its turn' : '');
  return {
    pct,
    stepNo: me.step ? RUN_STEPS.indexOf(me.step) + 1 : 0,
    stepText,
    detail: me.step === 'install' && inst?.detail ? inst.detail : '',
    eta: etaText(me.eta_seconds),
    done: me.step === 'verify',
  };
}

/** A firmware version string as the fleet page shortens it: the FW1 field
 *  of a CX7 version, or the tail of an SBP string. */
export function shortFirmware(s: string | null | undefined): string {
  if (!s) return 'not reported';
  const m = /FW1: ([\d.]+)/.exec(s);
  return m ? m[1] : s.replace(/^SBP:R:/, '').slice(0, 24);
}

export function bytes(b: number | null | undefined): string {
  if (b == null) return '';
  if (b > 1e9) return `${(b / 1e9).toFixed(1)} GB`;
  if (b > 1e6) return `${Math.round(b / 1e6)} MB`;
  return `${Math.round(b / 1e3)} KB`;
}

/** The last run's one-line verdict for a node, or null when there is nothing
 *  recent to say. `back` is the case the fleet page treats specially: the
 *  wait for the restart timed out, but the Spark is up on a new boot -- so
 *  the update almost certainly landed and one click can verify it. */
export interface LastRunLine {
  kind: 'ok' | 'rehearsal' | 'failed' | 'back' | 'stopped';
  tone: FleetTone;
  text: string;
  runId: string;
}

export function lastRunOf(n: FleetNode, now: number = Date.now()): LastRunLine | null {
  const r = n.last_run;
  if (!r || n.run || !recent(r, now)) return null;
  const me = r.nodes.find((x) => x.name === n.name);
  if (!me) return null;
  const when = clockOf(r.finished_at);
  if (me.status === 'ok' && r.rehearsal) {
    return {
      kind: 'rehearsal',
      tone: 'good',
      text: `Rehearsal passed at ${when} · nothing was changed`,
      runId: r.id,
    };
  }
  if (me.status === 'ok') {
    const c = me.changed ?? {};
    const rel = c.release ? `${c.release[0]} → ${c.release[1] || me.after?.release || ''}` : '';
    const upd = c.updates ? `${c.updates[0]} → ${c.updates[1]} package updates` : '';
    const note = me.release_note ? ` · ${me.release_note}` : '';
    return {
      kind: 'ok',
      tone: 'good',
      text: `Updated at ${when}` + [rel, upd].filter(Boolean).map((s) => ` · ${s}`).join('') + note,
      runId: r.id,
    };
  }
  if (me.status === 'failed') {
    const step = me.step ?? 'start';
    const back = Boolean(
      step === 'restart' &&
        n.reachable &&
        n.boot_id &&
        me.before?.boot_id &&
        n.boot_id !== me.before.boot_id,
    );
    if (back) {
      return {
        kind: 'back',
        tone: 'warning',
        text: `The install finished; the wait for the restart timed out · ${when} · ${n.name} is back on a new boot`,
        runId: r.id,
      };
    }
    const err = me.steps?.[step as RunStep]?.error || r.message;
    return {
      kind: 'failed',
      tone: 'critical',
      text: `Update stopped at ${step} · ${when} · ${err}`,
      runId: r.id,
    };
  }
  if (r.status === 'stopped') {
    return {
      kind: 'stopped',
      tone: 'warning',
      text: `Stopped as asked at ${when} after ${me.step ?? '—'}`,
      runId: r.id,
    };
  }
  return null;
}

export function clockOf(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—';
}

export function agoOf(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return '—';
  const s = (now - Date.parse(iso)) / 1000;
  if (s < 90) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

/** "Next check in: 12 minutes", or what to say instead. */
export function nextCheckText(f: Fleet | null, now: number = Date.now()): string {
  if (!f) return '';
  if (f.nodes.some((n) => n.checking)) return 'Checking now…';
  if (!f.next_sweep) return '';
  const mins = Math.round((Date.parse(f.next_sweep) - now) / 60000);
  return mins <= 0 ? 'Next check: any moment' : `Next check in: ${plural(mins, 'minute')}`;
}

/** The header line: "3 Sparks · 1 updating · 2 have updates · last checked 14:02". */
export function summaryOf(f: Fleet): string {
  const nodes = f.nodes;
  if (!nodes.length) return 'no Sparks yet';
  const withUpd = nodes.filter(installable).length;
  const waitingFw = nodes.filter(
    (n) =>
      n.reachable &&
      n.release?.available &&
      !installable(n) &&
      n.firmware_gap?.state === 'pending-vendor',
  ).length;
  const updating = nodes.filter((n) => n.run).length;
  return [
    plural(nodes.length, 'Spark'),
    updating ? `${updating} updating` : '',
    `${withUpd} ${withUpd === 1 ? 'has' : 'have'} updates`,
    waitingFw ? `${waitingFw} waiting on vendor firmware` : '',
    `last checked ${clockOf(f.last_sweep)}`,
  ]
    .filter(Boolean)
    .join(' · ');
}

/** "2 direct links · 200 GbE", for a cluster frame. */
export function clusterSummary(c: FleetCluster): string {
  const gbe = (l: FleetLink) => (l.speed_mbps ? `${Math.round(l.speed_mbps / 1000)} GbE` : 'link');
  if (!c.links.length) return 'linked';
  const speeds = [...new Set(c.links.map(gbe))].join(' + ');
  return `${c.links.length} direct ${c.links.length === 1 ? 'link' : 'links'} · ${speeds}`;
}

export function linkText(c: FleetCluster): string {
  const gbe = (l: FleetLink) => (l.speed_mbps ? `${Math.round(l.speed_mbps / 1000)} GbE` : 'link');
  return c.links
    .map((l) => `${l.a} ${l.a_port} ↔ ${l.b}${l.b_port ? ' ' + l.b_port : ''} · ${gbe(l)}`)
    .join('  ·  ');
}
