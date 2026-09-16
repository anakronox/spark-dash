/** The fleet updater's live state, and the actions the panel can take.
 *
 * Modelled on AlertFeed: one object the header button and the fly-out both
 * read, polled through `poll()` so a buried tab asks for nothing. Every
 * action here is a POST the backend forwards to spark-fleet-updates; the
 * fleet service does the work and this feed's next load shows the result.
 *
 * THREE CADENCES, the fleet page's own rule. 60s with the panel closed --
 * the header badge wants a number without anyone opening anything, and the
 * upstream answer is a file read. 10s with it open. 3s while any Spark is
 * being checked or updated, which is how the progress bar moves.
 */
import { fetchWithTimeout } from './request';
import { poll } from './visibility.svelte';
import type { Fleet, FleetEnvelope, FleetRun } from './fleet';
import { installable } from './fleet';

export const IDLE_MS = 60_000;
export const OPEN_MS = 10_000;
export const BUSY_MS = 3_000;

export class FleetFeed {
  /** The backend has a fleet service to talk to. False hides the button. */
  configured = $state(false);
  /** It answered. False with `configured` is "unreachable", not "empty". */
  available = $state(false);
  publicUrl = $state<string | null>(null);
  fleet = $state<Fleet | null>(null);
  loaded = $state(false);
  /** The panel is open: poll faster, because someone is looking. */
  open = $state(false);

  #stop: (() => void) | null = null;
  #period = 0;

  get withUpdates(): number {
    return (this.fleet?.nodes ?? []).filter(installable).length;
  }

  get updating(): number {
    return (this.fleet?.nodes ?? []).filter((n) => n.run).length;
  }

  get busy(): boolean {
    return (this.fleet?.nodes ?? []).some((n) => n.run || n.checking);
  }

  async load() {
    try {
      const resp = await fetchWithTimeout('/api/fleet');
      if (!resp.ok) throw new Error(String(resp.status));
      const body: FleetEnvelope = await resp.json();
      this.configured = body.configured;
      this.available = body.available;
      this.publicUrl = body.public_url;
      this.fleet = body.fleet;
    } catch {
      // The backend itself did not answer. Leave `configured` alone -- the
      // button should not vanish and reappear with every hiccup -- but the
      // fleet is unknown, not empty.
      this.available = false;
      this.fleet = null;
    } finally {
      this.loaded = true;
      this.#retune();
    }
  }

  /** Re-arm the poller when the right cadence changes. `poll()` owns one
   *  fixed period, so a change means a new one -- cheap, and rare. */
  #retune() {
    if (!this.#stop) return;
    const want = this.busy ? BUSY_MS : this.open ? OPEN_MS : IDLE_MS;
    if (want === this.#period) return;
    this.#stop();
    this.#period = want;
    this.#stop = poll(() => this.load(), want);
  }

  start() {
    this.load();
    this.#period = IDLE_MS;
    this.#stop = poll(() => this.load(), IDLE_MS);
  }

  stop() {
    this.#stop?.();
    this.#stop = null;
  }

  /** Called by the panel on open and close. Opening also loads at once: the
   *  idle poll may be most of a minute stale. */
  setOpen(open: boolean) {
    this.open = open;
    if (open) this.load();
    else this.#retune();
  }

  // --- actions ------------------------------------------------------------
  //
  // Each resolves when the fleet service has accepted the request, not when
  // the work is done; the next load shows the work. Errors carry the fleet
  // service's own wording, which the backend passes through as `detail`.

  async #post(path: string, body: object = {}): Promise<unknown> {
    const resp = await fetchWithTimeout(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const payload = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(payload?.detail || resp.statusText);
    return payload;
  }

  async checkAll() {
    await this.#post('/api/fleet/check');
    await this.load();
  }

  async check(name: string) {
    await this.#post(`/api/fleet/nodes/${encodeURIComponent(name)}/check`);
    await this.load();
  }

  /** Start an update; the fleet service updates every member of the Spark's
   *  cluster in turn. The password, if any, goes straight through and is
   *  never kept here. */
  async update(name: string, password?: string): Promise<FleetRun> {
    const run = (await this.#post(
      `/api/fleet/nodes/${encodeURIComponent(name)}/update`,
      password ? { password } : {},
    )) as FleetRun;
    await this.load();
    return run;
  }

  async rehearse(name: string, password?: string): Promise<FleetRun> {
    const run = (await this.#post(
      `/api/fleet/nodes/${encodeURIComponent(name)}/rehearse`,
      password ? { password } : {},
    )) as FleetRun;
    await this.load();
    return run;
  }

  async stopRun(runId: string) {
    await this.#post(`/api/fleet/runs/${encodeURIComponent(runId)}/stop`);
  }

  async verifyRun(runId: string) {
    await this.#post(`/api/fleet/runs/${encodeURIComponent(runId)}/verify`);
    await this.load();
  }

  /** The last 400 lines of a node's log for a run. Polled by the panel while
   *  the log is shown, at the busy cadence. */
  async log(runId: string, node: string): Promise<string[]> {
    const resp = await fetchWithTimeout(
      `/api/fleet/runs/${encodeURIComponent(runId)}/${encodeURIComponent(node)}/log`,
    );
    if (!resp.ok) throw new Error(String(resp.status));
    const body = await resp.json();
    return body.lines ?? [];
  }
}
