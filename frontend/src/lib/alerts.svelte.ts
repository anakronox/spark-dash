/** Current alert state, shared by everything that needs it.
 *
 * Lifted out of the banner component because three places now want it: the
 * banner itself, the header trigger (which shows a count), and the history
 * fly-out. Fetching it three times would triple the polling and let the three
 * disagree with each other for up to 30 seconds at a time.
 */

import { compositeKey, dedupeByKey } from './keys';
import { fetchWithTimeout } from './request';

export interface AlertItem {
  name: string;
  severity: string;
  summary: string;
  description: string;
  node: string | null;
  started_at: string | null;
  /** Every label, so a silence can be scoped to this exact alert instance
   *  rather than muting the same rule on every node. */
  labels: Record<string, string>;
}

export interface Silence {
  id: string;
  comment: string;
  createdBy: string;
  startsAt: string;
  endsAt: string;
  matchers: { name: string; value: string }[];
}

/** How long a silence can run. Matches the backend's cap — the failure mode of
 *  silencing is forgetting, so the options are deliberately short. */
export const SILENCE_DURATIONS = [
  { label: '1h', hours: 1 },
  { label: '4h', hours: 4 },
  { label: '24h', hours: 24 },
] as const;

export async function createSilence(
  labels: Record<string, string>,
  hours: number,
  comment: string,
): Promise<void> {
  const resp = await fetchWithTimeout('/api/alerts/silence', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ labels, hours, comment }),
  });
  if (!resp.ok) throw new Error(await resp.text());
}

export async function expireSilence(id: string): Promise<void> {
  const resp = await fetchWithTimeout(`/api/alerts/silence/${id}`, { method: 'DELETE' });
  if (!resp.ok) throw new Error(await resp.text());
}

export async function fetchSilences(): Promise<Silence[]> {
  const resp = await fetchWithTimeout('/api/alerts/silences');
  if (!resp.ok) throw new Error(String(resp.status));
  return (await resp.json()).silences ?? [];
}


/* KEYS FOR {#each}. These must be unique or Svelte throws each_key_duplicate,
 * and that throw ABORTS THE RENDER MID-UPDATE — it does not degrade, it leaves
 * whatever was on screen frozen in place.
 *
 * `alertname + node` is not unique. A rule fires once per INSTANCE, so a node
 * going down trips PrometheusTargetScrapeFailing for both its agent (:9500)
 * and its node-exporter (:9100) on the same scrape cycle — same rule, same
 * node, and in history the same `started_at` to the millisecond. Observed
 * 2026-08-16: the fly-out sat on "Loading…" with the data already fetched and
 * parsed, because the render that would have replaced it threw.
 *
 * The label set is what actually identifies an alert series, so key on that.
 */
const labelSignature = (labels: Record<string, string> | undefined): string =>
  labels && Object.keys(labels).length
    ? Object.keys(labels)
        .sort()
        .map((k) => `${k}=${labels[k]}`)
        .join(',')
    : '';

export const alertKey = (a: AlertItem): string =>
  labelSignature(a.labels) || compositeKey(a.name, a.node);

export const episodeKey = (e: AlertEpisode): string =>
  compositeKey(
    e.started_at,
    labelSignature(e.labels) || compositeKey(e.alertname, e.node),
  );


/** One continuous period an alert was pending and/or firing. */
export interface AlertEpisode {
  alertname: string;
  severity: string;
  node: string | null;
  started_at: number;
  ended_at: number;
  duration_s: number;
  ongoing: boolean;
  /** False when it went pending but never lasted long enough to fire. */
  fired: boolean;
  fired_at: number | null;
  labels: Record<string, string>;
}

export interface AlertSummary {
  episodes: number;
  fired: number;
  /** The number that says a rule is mistuned rather than its condition rare. */
  pending_only: number;
  ongoing: number;
}

export const HISTORY_RANGES = [
  { key: '24h', label: '24h', minutes: 60 * 24 },
  { key: '7d', label: '7d', minutes: 60 * 24 * 7 },
  { key: '30d', label: '30d', minutes: 60 * 24 * 30 },
] as const;

export class AlertFeed {
  available = $state(true);
  alerts = $state<AlertItem[]>([]);
  loaded = $state(false);

  /** Prometheus reachable but NOT recording. A distinct state from
   *  `!available`, and the more dangerous one: Alertmanager answers normally,
   *  so an empty alert list renders as reassurance over no data at all. */
  dataStale = $state(false);
  dataAgeS = $state<number | null>(null);

  #timer: ReturnType<typeof setInterval> | null = null;

  get critical(): number {
    return this.alerts.filter((a) => a.severity === 'critical').length;
  }

  /** Worst severity currently firing, for the trigger's colour. */
  get worst(): string | null {
    if (this.critical) return 'critical';
    return this.alerts.length ? this.alerts[0].severity : null;
  }

  async load() {
    try {
      const resp = await fetchWithTimeout('/api/alerts');
      if (!resp.ok) throw new Error(String(resp.status));
      const body = await resp.json();
      this.available = body.available;
      this.alerts = dedupeByKey(body.alerts ?? [], alertKey);
      this.dataStale = Boolean(body.data_stale);
      this.dataAgeS = body.data_age_s ?? null;
    } catch {
      // "Can't tell" is not "all clear" — the banner renders these
      // differently, and only one of them is reassuring.
      this.available = false;
      this.alerts = [];
      // Unknown, not "fine". The banner must not imply recording is healthy
      // when we could not ask.
      this.dataStale = true;
      this.dataAgeS = null;
    } finally {
      this.loaded = true;
    }
  }

  start() {
    this.load();
    // Alert state changes on Prometheus's evaluation interval, not the live
    // tick — polling faster would be load with no new information.
    this.#timer = setInterval(() => this.load(), 30_000);
  }

  stop() {
    if (this.#timer) clearInterval(this.#timer);
    this.#timer = null;
  }
}

/** Fetch episodes. Deliberately NOT polled — see AlertHistory. */
export async function fetchHistory(
  minutes: number,
  signal?: AbortSignal,
): Promise<{ episodes: AlertEpisode[]; summary: AlertSummary }> {
  const resp = await fetchWithTimeout(`/api/alerts/history?minutes=${minutes}`, { signal });
  if (!resp.ok) throw new Error(String(resp.status));
  const body = await resp.json();
  return { episodes: dedupeByKey(body.episodes ?? [], episodeKey), summary: body.summary };
}
