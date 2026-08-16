/** Current alert state, shared by everything that needs it.
 *
 * Lifted out of the banner component because three places now want it: the
 * banner itself, the header trigger (which shows a count), and the history
 * fly-out. Fetching it three times would triple the polling and let the three
 * disagree with each other for up to 30 seconds at a time.
 */

export interface AlertItem {
  name: string;
  severity: string;
  summary: string;
  description: string;
  node: string | null;
  started_at: string | null;
}

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
      const resp = await fetch('/api/alerts');
      if (!resp.ok) throw new Error(String(resp.status));
      const body = await resp.json();
      this.available = body.available;
      this.alerts = body.alerts ?? [];
    } catch {
      // "Can't tell" is not "all clear" — the banner renders these
      // differently, and only one of them is reassuring.
      this.available = false;
      this.alerts = [];
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
  const resp = await fetch(`/api/alerts/history?minutes=${minutes}`, { signal });
  if (!resp.ok) throw new Error(String(resp.status));
  const body = await resp.json();
  return { episodes: body.episodes ?? [], summary: body.summary };
}
