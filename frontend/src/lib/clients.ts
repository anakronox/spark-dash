/* Client stats from a LiteLLM gateway (roadmap AM): the backend's shapes, and
 * the wording Settings shows for them.
 *
 * Plain TypeScript, no runes and no DOM, so tests/js/clients.test.mjs runs it
 * under node. The feed that loads these lives in clients.svelte.ts.
 */

/** GET /api/clients/status. */
export interface ClientsStatus {
  /** AL5's first level: a gateway address is set. */
  capability: boolean;
  /** AL5's second level: the Settings switch. */
  enabled: boolean;
  /** Both: the gateway is scraped and the Clients card is on the page. */
  configured: boolean;
  /** The gateway's base address, `/v1` already stripped. */
  url: string | null;
  /** Why the `gateway:` block in cluster.yml could not be read. */
  error: string | null;
  /** False on a deployment whose nodes still come from SPARK_NODES. */
  cluster_file: boolean;
  /** /health's `litellm` value: ok, not scraped, off, not configured,
   *  unknown, or `invalid: <why>`. */
  state: string;
}

/** POST /api/clients/test. */
export interface ProbeResult {
  url: string;
  reachable: boolean;
  metrics: 'ok' | 'needs a key' | 'missing' | 'error' | 'not checked';
  /** One sentence, written by the backend, saying what to do next. */
  detail: string;
}

/** Settings' states, one branch each. */
export type ClientsView = 'no-cluster-file' | 'invalid' | 'absent' | 'off' | 'on';

export function viewOf(s: ClientsStatus | null): ClientsView {
  if (!s) return 'absent';
  if (!s.cluster_file) return 'no-cluster-file';
  if (s.error) return 'invalid';
  if (!s.capability) return 'absent';
  return s.enabled ? 'on' : 'off';
}

export interface Line {
  /** Settings has a warning tone and a plain one; good news is plain. */
  tone: 'warning' | '';
  text: string;
}

/** What the section says while the feature is on. */
export function stateLine(s: ClientsStatus): Line {
  switch (s.state) {
    case 'ok':
      return { tone: '', text: 'Prometheus is reading the gateway.' };
    case 'not scraped':
      return {
        tone: 'warning',
        text:
          'Prometheus is not reading the gateway. It picks up a new address within 30 seconds; ' +
          'if this stays, press Test.',
      };
    case 'unknown':
      return {
        tone: 'warning',
        text: 'Prometheus is not answering, so whether it is reading the gateway is unknown.',
      };
    default:
      return { tone: 'warning', text: s.state };
  }
}

export function probeLine(r: ProbeResult): Line {
  return { tone: r.metrics === 'ok' ? '' : 'warning', text: r.detail };
}

/** The address as the backend will store it, closely enough to tell whether
 *  the field differs from what is saved. The backend does the real parsing;
 *  this only stops "…:4000/v1" reading as a change from "…:4000". */
export function sameAddress(draft: string, saved: string | null): boolean {
  const norm = (u: string) => u.trim().replace(/\/+$/, '').replace(/\/v1$/, '');
  return norm(draft) === norm(saved ?? '');
}

// --- the Clients card (roadmap AM6) -------------------------------------------

/** One row of GET /api/clients: a client, speaking as one harness, to one
 *  model. */
export interface ClientRow {
  harness: { name: string; kind: 'harness' | 'sdk' | 'tool' | 'unknown' | 'unattributed' };
  user_agent: string | null;
  client: { ip: string | null; node: string | null; name: string | null; fqdn: string | null };
  /** Null when `rejected`: the request named a model no gateway route matched. */
  model: string | null;
  rejected: boolean;
  /** Null for a request that reached no engine. `node` is null for an
   *  endpoint the gateway fronts that this dashboard does not monitor. */
  engine: { server: string; node: string | null; runtime: string | null } | null;
  /** Attempts, failures included. */
  requests: number;
  failed: number;
  statuses: Record<string, number>;
  routes: string[];
  per_min: number;
  active: boolean;
}

export interface ClientsResponse {
  configured: boolean;
  window_minutes: number;
  /** Set when Prometheus began scraping the gateway inside the window, so
   *  nothing before this moment was counted. Epoch seconds. */
  counting_since: number | null;
  rows: ClientRow[];
}

/** Stable across polls, so a sorted table does not reshuffle its keys. */
export function rowKey(r: ClientRow): string {
  return [r.user_agent ?? '', r.client.ip ?? '', r.model ?? '', r.rejected ? 'x' : ''].join('|');
}

export function engineText(r: ClientRow): string {
  if (!r.engine) return '—';
  return r.engine.node ? `${r.engine.node} · ${r.engine.runtime}` : r.engine.server;
}

export function modelText(r: ClientRow): string {
  return r.rejected ? 'no such model' : (r.model ?? '—');
}

/** Why a harness name reads the way it does. */
export function harnessTitle(r: ClientRow): string {
  const raw = r.user_agent && r.user_agent !== 'None' ? r.user_agent : 'no User-Agent';
  switch (r.harness.kind) {
    case 'sdk':
      return `${raw}. A library's default User-Agent, which many programs send unchanged, so it cannot say which one; the Client column usually can.`;
    case 'unattributed':
      return 'LiteLLM drops the User-Agent from a failed /v1/messages request, so these failures cannot be matched to the client that sent them.';
    case 'unknown':
      return `${raw}. Not a harness the dashboard recognises yet.`;
    default:
      return raw;
  }
}

export function clientTitle(r: ClientRow): string {
  const who = r.client.node ? `node ${r.client.node}` : (r.client.fqdn ?? 'no reverse DNS');
  return r.client.ip ? `${who} · ${r.client.ip}` : who;
}

/** "3 × 500, 1 × 404", most common first. */
export function failedTitle(r: ClientRow): string {
  return Object.entries(r.statuses)
    .sort((a, b) => b[1] - a[1])
    .map(([status, n]) => `${n} × ${status}`)
    .join(', ');
}

/** The header's summary: distinct clients and total requests. */
export function summaryText(rows: ClientRow[]): string {
  if (!rows.length) return '';
  const machines = new Set(rows.map((r) => r.client.ip ?? r.client.name));
  const total = rows.reduce((n, r) => n + r.requests, 0);
  return `${machines.size} client${machines.size === 1 ? '' : 's'} · ${total} request${total === 1 ? '' : 's'}`;
}

/** "counting since 15:17", when the window reaches back before the scrape
 *  began; otherwise nothing. */
export function countingNote(since: number | null, locale?: string): string {
  if (since == null) return '';
  const t = new Date(since * 1000);
  const time = t.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' });
  return `counting since ${time}`;
}
