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
